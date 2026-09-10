from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from codes.checkpoint_protocol import (
    CHECKPOINT_STAGES,
    CHECKPOINT_STATUSES,
    checkpoint_stage_order_is_valid,
    checkpoint_relative_path,
)

from .database import connect_database, initialize_database, utc_now
from .errors import (
    IdempotencyConflictError,
    InvalidParameterError,
    JobNotCancelableError,
    JobNotFoundError,
    OptimisticLockConflictError,
)
from .job_state import JobState, validate_job_transition
from .path_security import validate_job_id


BASE_REQUEST_FIELDS = frozenset(
    {
        "upload_id",
        "paper_name",
        "domain",
        "eval_type",
        "generated_n",
        "auto_refine",
        "max_repair_rounds",
        "console_output",
        "skip_mineru",
        "pdf_markdown_path",
    }
)
COST_BUDGET_FIELDS = frozenset(
    {"cost_budget_policy", "cost_budget_currency", "cost_budget_amount"}
)
REQUEST_FIELDS = BASE_REQUEST_FIELDS | COST_BUDGET_FIELDS
EVENT_SOURCES = frozenset({"control_plane", "pipeline_adapter", "recovery", "worker"})
GLOBAL_WORKER_LEASE = "pipeline-worker"
CANCEL_DEDUPE_HASH = hashlib.sha256(b"cancel:v1").hexdigest()
DEFAULT_MAX_RECOVERIES = 1
EVENT_PAYLOAD_MAX_BYTES = 4096
EVENT_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LOCAL_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/(?:Users|home|tmp|var|mnt|opt)/)")
EXECUTION_STATUSES = frozenset({"queued", "running", "completed", "failed", "canceled"})
EVALUATION_STATUSES = frozenset({"pending", "running", "completed", "failed", "skipped"})
QUALITY_STATUSES = frozenset({"pending", "assessing", "accepted", "rejected", "skipped"})
JOB_COMMAND_TYPES = frozenset({"approve", "cancel", "retry", "repair"})
COMMAND_STATUSES = frozenset({"pending", "claimed", "completed", "failed", "rejected"})
COMMAND_REQUEST_STATUSES = frozenset({"accepted", "rejected"})
SENSITIVE_EVENT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "base_url",
        "command",
        "command_summary",
        "credential",
        "env",
        "environment",
        "full_response",
        "instance_token",
        "launch_token",
        "model_response",
        "pid",
        "process_create_time",
        "process_group_id",
        "prompt",
        "response",
        "response_body",
        "secret",
        "token",
    }
)
PROVIDER_SNAPSHOT_FIELDS = frozenset(
    {
        "reproduce_provider",
        "reproduce_model",
        "evaluation_provider",
        "evaluation_model",
        "evaluation_fallback_models",
        "provider_registry_version",
        "provider_contract_fingerprint",
    }
)


@dataclass(frozen=True)
class JobEventReplay:
    events: list[dict[str, Any]]
    gap_detected: bool
    available_event_count: int
    latest_event_id: int | None


def _future_utc(seconds: float) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _runtime_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{field} must contain between 1 and 256 characters.")
    if not value.isprintable():
        raise ValueError(f"{field} must contain printable characters.")
    return value


def _checkpoint_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("checkpoint_path must contain between 1 and 512 characters.")
    if not value.isascii() or not value.isprintable() or "\\" in value:
        raise ValueError("checkpoint_path must be a printable relative POSIX path.")
    if value.startswith("/") or ":" in value or ".." in value.split("/"):
        raise ValueError("checkpoint_path must be a printable relative POSIX path.")
    return value


REPAIR_ATTEMPT_STATUSES = frozenset({"running", "completed", "failed", "skipped"})


def _repair_attempt(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("repair attempt must be a positive integer.")
    return value


def _repair_text(value: str, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > 128 or not value.isprintable():
        raise ValueError(f"{field} must contain between 1 and 128 printable characters.")
    return value


def _repair_files_json(files_to_fix: list[str] | tuple[str, ...]) -> str:
    if (
        not isinstance(files_to_fix, (list, tuple))
        or len(files_to_fix) > 128
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 512
            or not item.isprintable()
            for item in files_to_fix
        )
    ):
        raise ValueError("files_to_fix must be a bounded list of printable paths.")
    encoded = json.dumps(
        list(files_to_fix),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    if len(encoded) > 8192:
        raise ValueError("files_to_fix is too large.")
    return encoded


def _repair_attempt_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["files_to_fix"] = json.loads(result.pop("files_to_fix_json"))
    return result


def _canonical_request(request: Mapping[str, object]) -> tuple[dict[str, object], str]:
    request_fields = set(request)
    if request_fields - REQUEST_FIELDS or not BASE_REQUEST_FIELDS.issubset(request_fields):
        raise ValueError("SQLite job requests must contain only the validated job fields.")
    budget_policy, budget_currency, budget_amount = _normalize_cost_budget(
        request.get("cost_budget_policy", "none"),
        request.get("cost_budget_currency"),
        request.get("cost_budget_amount"),
    )
    normalized = {field: request[field] for field in sorted(BASE_REQUEST_FIELDS)}
    normalized.update(
        {
            "cost_budget_policy": budget_policy,
            "cost_budget_currency": budget_currency,
            "cost_budget_amount": budget_amount,
        }
    )
    string_fields = (
        "upload_id",
        "paper_name",
        "domain",
        "eval_type",
        "console_output",
        "pdf_markdown_path",
    )
    if any(not isinstance(normalized[field], str) for field in string_fields):
        raise ValueError("SQLite job string fields must already be validated strings.")
    integer_fields = ("generated_n", "max_repair_rounds")
    if any(
        not isinstance(normalized[field], int) or isinstance(normalized[field], bool)
        for field in integer_fields
    ):
        raise ValueError("SQLite job integer fields must already be validated integers.")
    if any(
        not isinstance(normalized[field], bool)
        for field in ("auto_refine", "skip_mineru")
    ):
        raise ValueError("SQLite job boolean fields must already be validated booleans.")
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return normalized, hashlib.sha256(encoded).hexdigest()


def _normalize_cost_budget(
    policy: object,
    currency: object,
    amount: object,
) -> tuple[str, str | None, str | None]:
    if policy is None:
        policy = "none"
    if policy not in {"none", "hard"}:
        raise ValueError("cost_budget_policy must be 'none' or 'hard'.")
    if policy == "none":
        if currency is not None or amount is not None:
            raise ValueError("cost budget currency/amount require a hard policy.")
        return "none", None, None
    if (
        not isinstance(currency, str)
        or not 1 <= len(currency) <= 16
        or not currency.isascii()
        or not currency.isprintable()
    ):
        raise ValueError("cost_budget_currency must be printable ASCII.")
    if not isinstance(amount, str) or not 1 <= len(amount) <= 64:
        raise ValueError("cost_budget_amount must be a decimal string.")
    try:
        decimal_amount = Decimal(amount)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("cost_budget_amount must be a decimal string.") from exc
    if not decimal_amount.is_finite() or decimal_amount < 0:
        raise ValueError("cost_budget_amount must be finite and non-negative.")
    return "hard", currency, amount


def _idempotency_hash(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise InvalidParameterError(
            "Idempotency-Key must contain between 1 and 256 characters.",
            details={"header": "Idempotency-Key"},
        )
    if not value.isascii() or not value.isprintable():
        raise InvalidParameterError(
            "Idempotency-Key must contain printable ASCII characters.",
            details={"header": "Idempotency-Key"},
        )
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _required_idempotency_hash(value: str | None) -> str:
    key_hash = _idempotency_hash(value)
    if key_hash is None:
        raise InvalidParameterError(
            "Idempotency-Key is required for job commands.",
            details={"header": "Idempotency-Key"},
        )
    return key_hash


def _bounded_event_type(value: str) -> str:
    if not isinstance(value, str) or EVENT_TYPE_RE.fullmatch(value) is None:
        raise ValueError("event_type must be a printable event token.")
    return value


def _bounded_status(
    value: str | None,
    allowed: frozenset[str],
    field: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} is not allowed.")
    return value


def _command_type(value: str) -> str:
    if not isinstance(value, str) or value not in JOB_COMMAND_TYPES:
        raise InvalidParameterError(
            "command_type is not allowed.",
            details={"parameter": "command_type"},
        )
    return value


def _positive_command_id(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidParameterError(
            "command_id must be a positive integer.",
            details={"parameter": "command_id"},
        )
    return value


def _safe_event_payload_value(value: object, *, path: str = "payload") -> object:
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and LOCAL_PATH_RE.search(value):
            raise ValueError("event payload must not contain a local path.")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("event payload numbers must be finite.")
        return value
    if isinstance(value, list):
        if len(value) > 128:
            raise ValueError("event payload list is too large.")
        return [
            _safe_event_payload_value(item, path=f"{path}[]")
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > 128:
            raise ValueError("event payload object is too large.")
        sanitized: dict[str, object] = {}
        for raw_key, raw_item in value.items():
            if (
                not isinstance(raw_key, str)
                or not raw_key
                or len(raw_key) > 128
                or not raw_key.isascii()
                or not raw_key.isprintable()
            ):
                raise ValueError("event payload keys must be printable ASCII.")
            key = raw_key.lower()
            if (
                key in SENSITIVE_EVENT_KEYS
                or key.endswith("_path")
                or key.endswith("_token")
                or key.startswith("prompt")
            ):
                raise ValueError("event payload contains sensitive metadata.")
            sanitized[raw_key] = _safe_event_payload_value(
                raw_item,
                path=f"{path}.{raw_key}",
            )
        return sanitized
    raise ValueError("event payload contains an unsupported value.")


def _event_payload_json(payload: Mapping[str, object] | None) -> str | None:
    if payload is None:
        return None
    sanitized = _safe_event_payload_value(dict(payload))
    encoded = json.dumps(
        sanitized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > EVENT_PAYLOAD_MAX_BYTES:
        raise ValueError("event payload is too large.")
    return encoded


def _command_event_payload(
    *,
    command_id: int,
    command_type: str,
    command_status: str,
    reason: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "command_id": command_id,
        "command_type": command_type,
        "command_status": command_status,
    }
    if reason:
        payload["reason"] = reason
    return payload


def _normalize_provider_snapshot(
    snapshot: Mapping[str, object] | None,
) -> dict[str, object | None]:
    if snapshot is None:
        return {field: None for field in PROVIDER_SNAPSHOT_FIELDS}
    if not isinstance(snapshot, Mapping) or set(snapshot) != PROVIDER_SNAPSHOT_FIELDS:
        raise ValueError("Provider selection snapshot has invalid fields.")
    normalized = dict(snapshot)
    for field in (
        "reproduce_provider",
        "reproduce_model",
        "evaluation_provider",
        "evaluation_model",
    ):
        value = normalized[field]
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            or not value.isascii()
            or not value.isprintable()
        ):
            raise ValueError("Provider selection IDs must be printable ASCII strings.")
    fallbacks = normalized["evaluation_fallback_models"]
    if (
        not isinstance(fallbacks, (list, tuple))
        or len(fallbacks) > 32
        or not all(
            isinstance(model_id, str)
            and 1 <= len(model_id) <= 128
            and model_id.isascii()
            and model_id.isprintable()
            for model_id in fallbacks
        )
        or len(set(fallbacks)) != len(fallbacks)
    ):
        raise ValueError("Evaluation fallback model IDs are invalid.")
    version = normalized["provider_registry_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("Provider registry version must be a positive integer.")
    fingerprint = normalized["provider_contract_fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise ValueError("Provider contract fingerprint must be lowercase SHA-256.")
    normalized["evaluation_fallback_models"] = list(fallbacks)
    return normalized


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for field in ("auto_refine", "skip_mineru"):
        if field in result:
            result[field] = bool(result[field])
    result.pop("idempotency_key_hash", None)
    result.pop("request_hash", None)
    if "evaluation_fallback_models_json" in result:
        encoded_fallbacks = result.pop("evaluation_fallback_models_json")
        result["evaluation_fallback_models"] = (
            None if encoded_fallbacks is None else json.loads(encoded_fallbacks)
        )
    return result


def _command_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result.pop("dedupe_key_hash", None)
    result["command_id"] = int(result["id"])
    return result


class JobRepository:
    def __init__(self, database_path: Path | str | None = None) -> None:
        self.database_path = initialize_database(database_path)

    def create_job(
        self,
        *,
        job_id: str,
        request: Mapping[str, object],
        paper_name: str,
        idempotency_key: str | None = None,
        provider_snapshot: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        job_id = validate_job_id(job_id)
        normalized, request_hash = _canonical_request(request)
        key_hash = _idempotency_hash(idempotency_key)
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if key_hash is not None:
                    existing = connection.execute(
                        "SELECT * FROM jobs WHERE idempotency_key_hash = ?",
                        (key_hash,),
                    ).fetchone()
                    if existing is not None:
                        if existing["request_hash"] != request_hash:
                            raise IdempotencyConflictError(
                                details={"header": "Idempotency-Key"}
                            )
                        connection.commit()
                        return _row_to_dict(existing), False

                snapshot = _normalize_provider_snapshot(provider_snapshot)
                fallback_models_json = (
                    None
                    if snapshot["evaluation_fallback_models"] is None
                    else json.dumps(
                        snapshot["evaluation_fallback_models"],
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                )

                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, idempotency_key_hash, request_hash, paper_name,
                        upload_id, domain, eval_type, generated_n, auto_refine,
                        max_repair_rounds, console_output, skip_mineru,
                        pdf_markdown_path, execution_status, evaluation_status,
                        quality_status, version, created_at, updated_at,
                        cost_budget_policy, cost_budget_currency, cost_budget_amount,
                        reproduce_provider, reproduce_model, evaluation_provider,
                        evaluation_model, evaluation_fallback_models_json,
                        provider_registry_version, provider_contract_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        key_hash,
                        request_hash,
                        paper_name,
                        normalized["upload_id"],
                        normalized["domain"],
                        normalized["eval_type"],
                        normalized["generated_n"],
                        int(bool(normalized["auto_refine"])),
                        normalized["max_repair_rounds"],
                        normalized["console_output"],
                        int(bool(normalized["skip_mineru"])),
                        normalized["pdf_markdown_path"],
                        "queued",
                        "pending",
                        "pending",
                        1,
                        now,
                        now,
                        normalized["cost_budget_policy"],
                        normalized["cost_budget_currency"],
                        normalized["cost_budget_amount"],
                        snapshot["reproduce_provider"],
                        snapshot["reproduce_model"],
                        snapshot["evaluation_provider"],
                        snapshot["evaluation_model"],
                        fallback_models_json,
                        snapshot["provider_registry_version"],
                        snapshot["provider_contract_fingerprint"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, event_type, source, job_version,
                        execution_status, evaluation_status, quality_status, created_at
                    ) VALUES (?, 'job.created', 'control_plane', 1, ?, ?, ?, ?)
                    """,
                    (job_id, "queued", "pending", "pending", now),
                )
                created = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                connection.commit()
                return _row_to_dict(created), True
            except Exception:
                connection.rollback()
                raise

    def find_idempotent_job(
        self,
        *,
        request: Mapping[str, object],
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
        if idempotency_key is None:
            return None
        _, request_hash = _canonical_request(request)
        key_hash = _idempotency_hash(idempotency_key)
        with closing(connect_database(self.database_path)) as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key_hash = ?",
                (key_hash,),
            ).fetchone()
        if existing is None:
            return None
        if existing["request_hash"] != request_hash:
            raise IdempotencyConflictError(details={"header": "Idempotency-Key"})
        return _row_to_dict(existing)

    def get_job(self, job_id: str) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        with closing(connect_database(self.database_path)) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError()
        return _row_to_dict(row)

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 200))
        with closing(connect_database(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC, job_id DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_shutdown_blockers(self) -> list[dict[str, Any]]:
        with closing(connect_database(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT job_id, execution_status, recovery_status, updated_at
                FROM jobs
                WHERE execution_status NOT IN ('completed', 'failed', 'canceled')
                   OR recovery_status IN ('prepared', 'running')
                ORDER BY updated_at DESC, job_id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _insert_job_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event_type: str,
        source: str,
        job_version: int,
        execution_status: str | None = None,
        evaluation_status: str | None = None,
        quality_status: str | None = None,
        payload: Mapping[str, object] | None = None,
        created_at: str | None = None,
    ) -> int:
        event_type = _bounded_event_type(event_type)
        if source not in EVENT_SOURCES:
            raise ValueError("source is not an allowed job event source.")
        if (
            not isinstance(job_version, int)
            or isinstance(job_version, bool)
            or job_version < 1
        ):
            raise ValueError("job_version must be a positive integer.")
        execution_status = _bounded_status(
            execution_status,
            EXECUTION_STATUSES,
            "execution_status",
        )
        evaluation_status = _bounded_status(
            evaluation_status,
            EVALUATION_STATUSES,
            "evaluation_status",
        )
        quality_status = _bounded_status(
            quality_status,
            QUALITY_STATUSES,
            "quality_status",
        )
        cursor = connection.execute(
            """
            INSERT INTO job_events (
                job_id, event_type, source, job_version,
                execution_status, evaluation_status, quality_status,
                created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                event_type,
                source,
                job_version,
                execution_status,
                evaluation_status,
                quality_status,
                created_at or utc_now(),
                _event_payload_json(payload),
            ),
        )
        return int(cursor.lastrowid)

    def record_job_event(
        self,
        job_id: str,
        *,
        event_type: str,
        source: str,
        job_version: int,
        execution_status: str | None = None,
        evaluation_status: str | None = None,
        quality_status: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                job = connection.execute(
                    "SELECT 1 FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if job is None:
                    raise JobNotFoundError()
                event_id = self._insert_job_event(
                    connection,
                    job_id=job_id,
                    event_type=event_type,
                    source=source,
                    job_version=job_version,
                    execution_status=execution_status,
                    evaluation_status=evaluation_status,
                    quality_status=quality_status,
                    payload=payload,
                    created_at=now,
                )
                row = connection.execute(
                    "SELECT * FROM job_events WHERE id = ?",
                    (event_id,),
                ).fetchone()
                connection.commit()
                return dict(row)
            except Exception:
                connection.rollback()
                raise

    def list_job_events_after(
        self,
        job_id: str,
        last_event_id: int,
        *,
        limit: int,
    ) -> JobEventReplay:
        job_id = validate_job_id(job_id)
        if (
            not isinstance(last_event_id, int)
            or isinstance(last_event_id, bool)
            or last_event_id < 0
        ):
            raise ValueError("last_event_id must be a non-negative integer.")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer.")
        bounded_limit = min(limit, 500)
        with closing(connect_database(self.database_path)) as connection:
            job = connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise JobNotFoundError()
            summary = connection.execute(
                """
                SELECT COUNT(*) AS count, MAX(id) AS latest_event_id
                FROM job_events
                WHERE job_id = ? AND id > ?
                """,
                (job_id, last_event_id),
            ).fetchone()
            available = int(summary["count"])
            latest = summary["latest_event_id"]
            if available > bounded_limit:
                return JobEventReplay(
                    events=[],
                    gap_detected=True,
                    available_event_count=available,
                    latest_event_id=None if latest is None else int(latest),
                )
            rows = connection.execute(
                """
                SELECT *
                FROM job_events
                WHERE job_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (job_id, last_event_id, bounded_limit),
            ).fetchall()
        return JobEventReplay(
            events=[dict(row) for row in rows],
            gap_detected=False,
            available_event_count=available,
            latest_event_id=None if latest is None else int(latest),
        )

    def acquire_worker_lease(
        self,
        worker_id: str,
        instance_token: str,
        *,
        lease_seconds: float = 15.0,
    ) -> bool:
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive.")
        now = utc_now()
        expires_at = _future_utc(lease_seconds)
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                lease = connection.execute(
                    "SELECT * FROM worker_leases WHERE lease_name = ?",
                    (GLOBAL_WORKER_LEASE,),
                ).fetchone()
                if lease is None:
                    connection.execute(
                        """
                        INSERT INTO worker_leases (
                            lease_name, owner_id, owner_token, expires_at, version,
                            acquired_at, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            GLOBAL_WORKER_LEASE,
                            worker_id,
                            instance_token,
                            expires_at,
                            now,
                            now,
                        ),
                    )
                    connection.commit()
                    return True
                same_instance = (
                    lease["owner_id"] == worker_id
                    and lease["owner_token"] == instance_token
                )
                if not same_instance and lease["expires_at"] > now:
                    connection.rollback()
                    return False
                acquired_at = (
                    lease["acquired_at"]
                    if same_instance and lease["expires_at"] > now
                    else now
                )
                connection.execute(
                    """
                    UPDATE worker_leases
                    SET owner_id = ?, owner_token = ?, expires_at = ?,
                        version = version + 1,
                        acquired_at = ?, updated_at = ?
                    WHERE lease_name = ?
                    """,
                    (
                        worker_id,
                        instance_token,
                        expires_at,
                        acquired_at,
                        now,
                        GLOBAL_WORKER_LEASE,
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def release_worker_lease(self, worker_id: str, instance_token: str) -> bool:
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        with closing(connect_database(self.database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    DELETE FROM worker_leases
                    WHERE lease_name = ? AND owner_id = ? AND owner_token = ?
                    """,
                    (GLOBAL_WORKER_LEASE, worker_id, instance_token),
                )
        return cursor.rowcount == 1

    @staticmethod
    def _assert_current_lease(
        connection: sqlite3.Connection,
        worker_id: str,
        instance_token: str,
        now: str,
    ) -> None:
        lease = connection.execute(
            """
            SELECT 1 FROM worker_leases
            WHERE lease_name = ? AND owner_id = ? AND owner_token = ?
              AND expires_at > ?
            """,
            (GLOBAL_WORKER_LEASE, worker_id, instance_token, now),
        ).fetchone()
        if lease is None:
            raise RuntimeError("The worker does not hold the global lease.")

    def claim_next_queued_job(
        self,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
    ) -> dict[str, Any] | None:
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        launch_token = _runtime_identifier(launch_token, "launch_token")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                running = connection.execute(
                    "SELECT 1 FROM jobs WHERE execution_status = 'running' LIMIT 1"
                ).fetchone()
                if running is not None:
                    connection.commit()
                    return None
                current = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE execution_status = 'queued'
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT 1
                    """
                ).fetchone()
                if current is None:
                    connection.commit()
                    return None
                next_version = int(current["version"]) + 1
                connection.execute(
                    """
                    UPDATE jobs
                    SET execution_status = 'running', failure_code = NULL,
                        version = ?, updated_at = ?
                    WHERE job_id = ? AND execution_status = 'queued'
                    """,
                    (next_version, now, current["job_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO job_processes (
                        job_id, worker_id, launch_token, launch_state, heartbeat_at
                    ) VALUES (?, ?, ?, 'claimed', ?)
                    """,
                    (current["job_id"], worker_id, launch_token, now),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, event_type, source, job_version,
                        execution_status, evaluation_status, quality_status, created_at
                    ) VALUES (?, 'job.claimed', 'worker', ?, 'running', ?, ?, ?)
                    """,
                    (
                        current["job_id"],
                        next_version,
                        current["evaluation_status"],
                        current["quality_status"],
                        now,
                    ),
                )
                claimed = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (current["job_id"],)
                ).fetchone()
                connection.commit()
                return _row_to_dict(claimed)
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _is_identity_unresolved(
        connection: sqlite3.Connection,
        job_id: str,
        execution_status: str,
    ) -> bool:
        if execution_status != "running":
            return False
        process = connection.execute(
            "SELECT launch_state FROM job_processes WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return bool(
            process is not None
            and process["launch_state"] == "identity_unresolved"
        )

    @classmethod
    def _command_rejection_reason(
        cls,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        command_type: str,
    ) -> str | None:
        execution_status = str(job["execution_status"])
        if cls._is_identity_unresolved(
            connection,
            str(job["job_id"]),
            execution_status,
        ):
            return "process_identity_unresolved"
        if command_type == "cancel":
            if execution_status in {"completed", "failed", "canceled"}:
                return "already_finished"
            if execution_status not in {"queued", "running"}:
                return "invalid_state"
            return None
        if command_type == "retry":
            return None if execution_status in {"failed", "canceled"} else "invalid_state"
        if command_type in {"approve", "repair"}:
            allowed = (
                execution_status == "completed"
                and job["evaluation_status"] == "completed"
                and job["quality_status"] == "rejected"
            )
            return None if allowed else "invalid_state"
        return "invalid_command"

    def _request_job_command_with_hash(
        self,
        job_id: str,
        *,
        command_type: str,
        dedupe_key_hash: str,
    ) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        command_type = _command_type(command_type)
        if (
            not isinstance(dedupe_key_hash, str)
            or len(dedupe_key_hash) != 64
            or any(character not in "0123456789abcdef" for character in dedupe_key_hash)
        ):
            raise InvalidParameterError(
                "Idempotency-Key is invalid.",
                details={"header": "Idempotency-Key"},
            )
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                job = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if job is None:
                    raise JobNotFoundError()
                existing = connection.execute(
                    """
                    SELECT * FROM job_commands
                    WHERE job_id = ? AND dedupe_key_hash = ?
                    """,
                    (job_id, dedupe_key_hash),
                ).fetchone()
                if existing is not None:
                    if existing["command_type"] != command_type:
                        raise IdempotencyConflictError(
                            details={"header": "Idempotency-Key"}
                        )
                    connection.commit()
                    return _command_row_to_dict(existing)

                reason = self._command_rejection_reason(
                    connection,
                    job,
                    command_type,
                )
                request_status = "rejected" if reason else "accepted"
                status = "rejected" if reason else "pending"
                connection.execute(
                    """
                    INSERT INTO job_commands (
                        job_id, command_type, status, dedupe_key_hash,
                        version, created_at, completed_at, error_code,
                        request_status, rejection_code, result_code, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        command_type,
                        status,
                        dedupe_key_hash,
                        now,
                        now if reason else None,
                        reason,
                        request_status,
                        reason,
                        reason,
                        now,
                    ),
                )
                command_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                self._insert_job_event(
                    connection,
                    job_id=job_id,
                    event_type=(
                        "job.command_rejected"
                        if reason
                        else "job.command_requested"
                    ),
                    source="control_plane",
                    job_version=int(job["version"]),
                    execution_status=job["execution_status"],
                    evaluation_status=job["evaluation_status"],
                    quality_status=job["quality_status"],
                    payload=_command_event_payload(
                        command_id=command_id,
                        command_type=command_type,
                        command_status=status,
                        reason=reason,
                    ),
                    created_at=now,
                )
                command = connection.execute(
                    "SELECT * FROM job_commands WHERE id = ?",
                    (command_id,),
                ).fetchone()
                connection.commit()
                return _command_row_to_dict(command)
            except Exception:
                connection.rollback()
                raise

    def request_job_command(
        self,
        job_id: str,
        *,
        command_type: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        return self._request_job_command_with_hash(
            job_id,
            command_type=command_type,
            dedupe_key_hash=_required_idempotency_hash(idempotency_key),
        )

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        command = self._request_job_command_with_hash(
            job_id,
            command_type="cancel",
            dedupe_key_hash=CANCEL_DEDUPE_HASH,
        )
        if command["status"] in {"rejected", "failed"}:
            reason = str(
                command.get("rejection_code")
                or command.get("error_code")
                or "invalid_state"
            )
            raise JobNotCancelableError(
                reason,
                (
                    "Pipeline process identity is unresolved and cannot be canceled safely."
                    if reason == "process_identity_unresolved"
                    else "Job is already finished and cannot be canceled."
                    if reason == "already_finished"
                    else "Job cannot be canceled."
                ),
            )
        return command

    def get_cancel_command(self, job_id: str) -> dict[str, Any] | None:
        job_id = validate_job_id(job_id)
        with closing(connect_database(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT * FROM job_commands
                WHERE job_id = ? AND command_type = 'cancel'
                ORDER BY id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return _command_row_to_dict(row) if row is not None else None

    def get_job_command(self, job_id: str, command_id: int) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        command_id = _positive_command_id(command_id)
        with closing(connect_database(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT * FROM job_commands
                WHERE job_id = ? AND id = ?
                """,
                (job_id, command_id),
            ).fetchone()
        if row is None:
            raise JobNotFoundError()
        return _command_row_to_dict(row)

    def list_job_commands(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        job_id = validate_job_id(job_id)
        bounded_limit = max(1, min(int(limit), 200))
        with closing(connect_database(self.database_path)) as connection:
            job = connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise JobNotFoundError()
            rows = connection.execute(
                """
                SELECT * FROM job_commands
                WHERE job_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (job_id, bounded_limit),
            ).fetchall()
        return [_command_row_to_dict(row) for row in rows]

    def next_control_command_type(
        self,
        *,
        worker_id: str,
        instance_token: str,
    ) -> str | None:
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            self._assert_current_lease(connection, worker_id, instance_token, now)
            row = connection.execute(
                """
                SELECT command_type
                FROM job_commands
                WHERE command_type IN ('approve', 'repair', 'retry')
                  AND status IN ('pending', 'claimed')
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()
        return None if row is None else str(row["command_type"])

    def cancel_next_queued_job(
        self,
        worker_id: str,
        instance_token: str,
    ) -> dict[str, Any] | None:
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                row = connection.execute(
                    """
                    SELECT c.id AS command_id, j.*
                    FROM job_commands AS c
                    JOIN jobs AS j ON j.job_id = c.job_id
                    WHERE c.command_type = 'cancel'
                      AND c.status IN ('pending', 'claimed')
                      AND j.execution_status = 'queued'
                    ORDER BY c.id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                next_version = int(row["version"]) + 1
                connection.execute(
                    """
                    UPDATE jobs
                    SET execution_status = 'canceled', version = ?, updated_at = ?
                    WHERE job_id = ? AND execution_status = 'queued'
                    """,
                    (next_version, now, row["job_id"]),
                )
                connection.execute(
                    """
                    UPDATE job_commands
                    SET status = 'completed', claimed_by_worker_id = ?,
                        claimed_at = COALESCE(claimed_at, ?), completed_at = ?,
                        result_code = 'canceled', updated_at = ?,
                        version = version + 1
                    WHERE id = ?
                    """,
                    (worker_id, now, now, now, row["command_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, event_type, source, job_version,
                        execution_status, evaluation_status, quality_status, created_at
                    ) VALUES (?, 'job.canceled', 'worker', ?, 'canceled', ?, ?, ?)
                    """,
                    (
                        row["job_id"],
                        next_version,
                        row["evaluation_status"],
                        row["quality_status"],
                        now,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
                ).fetchone()
                connection.commit()
                return _row_to_dict(updated)
            except Exception:
                connection.rollback()
                raise

    def claim_cancel_command(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
    ) -> dict[str, Any] | None:
        job_id = validate_job_id(job_id)
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                command = connection.execute(
                    """
                    SELECT * FROM job_commands
                    WHERE job_id = ? AND command_type = 'cancel'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                if command is None:
                    connection.commit()
                    return None
                if command["status"] in {"pending", "claimed"}:
                    connection.execute(
                        """
                        UPDATE job_commands
                        SET status = 'claimed', claimed_by_worker_id = ?,
                            claimed_at = COALESCE(claimed_at, ?),
                            updated_at = ?, version = version + 1
                        WHERE id = ?
                        """,
                        (worker_id, now, now, command["id"]),
                    )
                    command = connection.execute(
                        "SELECT * FROM job_commands WHERE id = ?", (command["id"],)
                    ).fetchone()
                connection.commit()
                return _command_row_to_dict(command)
            except Exception:
                connection.rollback()
                raise

    def _mark_command_failed_locked(
        self,
        connection: sqlite3.Connection,
        *,
        command_id: int,
        job_id: str,
        command_type: str,
        job_version: int,
        execution_status: str,
        evaluation_status: str,
        quality_status: str,
        error_code: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE job_commands
            SET status = 'failed', error_code = ?, result_code = ?,
                completed_at = ?, updated_at = ?, version = version + 1
            WHERE id = ? AND status IN ('pending', 'claimed')
            """,
            (error_code, error_code, now, now, command_id),
        )
        self._insert_job_event(
            connection,
            job_id=job_id,
            event_type="job.command_failed",
            source="worker",
            job_version=job_version,
            execution_status=execution_status,
            evaluation_status=evaluation_status,
            quality_status=quality_status,
            payload=_command_event_payload(
                command_id=command_id,
                command_type=command_type,
                command_status="failed",
                reason=error_code,
            ),
            created_at=now,
        )

    def apply_next_retry_command(
        self,
        *,
        worker_id: str,
        instance_token: str,
    ) -> dict[str, Any] | None:
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                row = connection.execute(
                    """
                    SELECT c.id AS command_id, c.version AS command_version,
                           c.command_type, j.*
                    FROM job_commands AS c
                    JOIN jobs AS j ON j.job_id = c.job_id
                    WHERE c.command_type = 'retry'
                      AND c.status IN ('pending', 'claimed')
                    ORDER BY c.id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                connection.execute(
                    """
                    UPDATE job_commands
                    SET status = 'claimed', claimed_by_worker_id = ?,
                        claimed_at = COALESCE(claimed_at, ?),
                        updated_at = ?, version = version + 1
                    WHERE id = ? AND status IN ('pending', 'claimed')
                    """,
                    (worker_id, now, now, row["command_id"]),
                )
                process = connection.execute(
                    "SELECT * FROM job_processes WHERE job_id = ?",
                    (row["job_id"],),
                ).fetchone()
                if row["execution_status"] not in {"failed", "canceled"} or (
                    process is not None and process["launch_state"] != "exited"
                ):
                    self._mark_command_failed_locked(
                        connection,
                        command_id=int(row["command_id"]),
                        job_id=row["job_id"],
                        command_type="retry",
                        job_version=int(row["version"]),
                        execution_status=row["execution_status"],
                        evaluation_status=row["evaluation_status"],
                        quality_status=row["quality_status"],
                        error_code="state_changed",
                        now=now,
                    )
                    connection.commit()
                    return None
                if process is not None:
                    connection.execute(
                        "DELETE FROM job_processes WHERE job_id = ?",
                        (row["job_id"],),
                    )
                next_version = int(row["version"]) + 1
                connection.execute(
                    """
                    UPDATE jobs
                    SET execution_status = 'queued',
                        evaluation_status = 'pending',
                        quality_status = 'pending',
                        failure_code = NULL,
                        recovery_count = 0,
                        recovery_status = 'none',
                        recovery_error_code = NULL,
                        current_stage = NULL,
                        current_stage_attempt = NULL,
                        last_checkpoint_stage = NULL,
                        version = ?, updated_at = ?
                    WHERE job_id = ? AND version = ?
                    """,
                    (next_version, now, row["job_id"], row["version"]),
                )
                connection.execute(
                    """
                    UPDATE job_commands
                    SET status = 'completed', result_code = 'retry_queued',
                        completed_at = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, now, row["command_id"]),
                )
                self._insert_job_event(
                    connection,
                    job_id=row["job_id"],
                    event_type="job.retry_queued",
                    source="worker",
                    job_version=next_version,
                    execution_status="queued",
                    evaluation_status="pending",
                    quality_status="pending",
                    payload=_command_event_payload(
                        command_id=int(row["command_id"]),
                        command_type="retry",
                        command_status="completed",
                    ),
                    created_at=now,
                )
                command = connection.execute(
                    "SELECT * FROM job_commands WHERE id = ?",
                    (row["command_id"],),
                ).fetchone()
                connection.commit()
                return _command_row_to_dict(command)
            except Exception:
                connection.rollback()
                raise

    def apply_next_approve_command(
        self,
        *,
        worker_id: str,
        instance_token: str,
    ) -> dict[str, Any] | None:
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                row = connection.execute(
                    """
                    SELECT c.id AS command_id, j.*
                    FROM job_commands AS c
                    JOIN jobs AS j ON j.job_id = c.job_id
                    WHERE c.command_type = 'approve'
                      AND c.status IN ('pending', 'claimed')
                    ORDER BY c.id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                connection.execute(
                    """
                    UPDATE job_commands
                    SET status = 'claimed', claimed_by_worker_id = ?,
                        claimed_at = COALESCE(claimed_at, ?),
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (worker_id, now, now, row["command_id"]),
                )
                if (
                    row["execution_status"] != "completed"
                    or row["evaluation_status"] != "completed"
                    or row["quality_status"] != "rejected"
                ):
                    self._mark_command_failed_locked(
                        connection,
                        command_id=int(row["command_id"]),
                        job_id=row["job_id"],
                        command_type="approve",
                        job_version=int(row["version"]),
                        execution_status=row["execution_status"],
                        evaluation_status=row["evaluation_status"],
                        quality_status=row["quality_status"],
                        error_code="state_changed",
                        now=now,
                    )
                    connection.commit()
                    return None
                next_version = int(row["version"]) + 1
                connection.execute(
                    """
                    UPDATE jobs
                    SET quality_status = 'accepted', version = ?, updated_at = ?
                    WHERE job_id = ? AND version = ?
                    """,
                    (next_version, now, row["job_id"], row["version"]),
                )
                connection.execute(
                    """
                    UPDATE job_commands
                    SET status = 'completed', result_code = 'approved',
                        completed_at = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, now, row["command_id"]),
                )
                self._insert_job_event(
                    connection,
                    job_id=row["job_id"],
                    event_type="job.approved",
                    source="worker",
                    job_version=next_version,
                    execution_status="completed",
                    evaluation_status="completed",
                    quality_status="accepted",
                    payload=_command_event_payload(
                        command_id=int(row["command_id"]),
                        command_type="approve",
                        command_status="completed",
                    ),
                    created_at=now,
                )
                command = connection.execute(
                    "SELECT * FROM job_commands WHERE id = ?",
                    (row["command_id"],),
                ).fetchone()
                connection.commit()
                return _command_row_to_dict(command)
            except Exception:
                connection.rollback()
                raise

    def apply_next_repair_command(
        self,
        *,
        worker_id: str,
        instance_token: str,
    ) -> dict[str, Any] | None:
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                row = connection.execute(
                    """
                    SELECT c.id AS command_id, c.command_type, j.*
                    FROM job_commands AS c
                    JOIN jobs AS j ON j.job_id = c.job_id
                    WHERE c.command_type = 'repair'
                      AND c.status IN ('pending', 'claimed')
                    ORDER BY c.id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                connection.execute(
                    """
                    UPDATE job_commands
                    SET status = 'claimed', claimed_by_worker_id = ?,
                        claimed_at = COALESCE(claimed_at, ?),
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (worker_id, now, now, row["command_id"]),
                )
                if (
                    row["execution_status"] != "completed"
                    or row["evaluation_status"] != "completed"
                    or row["quality_status"] != "rejected"
                ):
                    self._mark_command_failed_locked(
                        connection,
                        command_id=int(row["command_id"]),
                        job_id=row["job_id"],
                        command_type="repair",
                        job_version=int(row["version"]),
                        execution_status=row["execution_status"],
                        evaluation_status=row["evaluation_status"],
                        quality_status=row["quality_status"],
                        error_code="state_changed",
                        now=now,
                    )
                    connection.commit()
                    return None
                connection.execute(
                    """
                    UPDATE job_commands
                    SET status = 'completed', result_code = 'repair_requested',
                        completed_at = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, now, row["command_id"]),
                )
                self._insert_job_event(
                    connection,
                    job_id=row["job_id"],
                    event_type="job.repair_requested",
                    source="worker",
                    job_version=int(row["version"]),
                    execution_status=row["execution_status"],
                    evaluation_status=row["evaluation_status"],
                    quality_status=row["quality_status"],
                    payload=_command_event_payload(
                        command_id=int(row["command_id"]),
                        command_type="repair",
                        command_status="completed",
                    ),
                    created_at=now,
                )
                command = connection.execute(
                    "SELECT * FROM job_commands WHERE id = ?",
                    (row["command_id"],),
                ).fetchone()
                connection.commit()
                return _command_row_to_dict(command)
            except Exception:
                connection.rollback()
                raise

    def claim_next_repair_command(
        self,
        *,
        worker_id: str,
        instance_token: str,
    ) -> dict[str, Any] | None:
        return self.apply_next_repair_command(
            worker_id=worker_id,
            instance_token=instance_token,
        )

    def record_process_started(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        pid: int,
        process_create_time: str,
        process_group_id: int,
        command_summary: str,
    ) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        launch_token = _runtime_identifier(launch_token, "launch_token")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("pid must be a positive integer.")
        if (
            not isinstance(process_group_id, int)
            or isinstance(process_group_id, bool)
            or process_group_id <= 0
        ):
            raise ValueError("process_group_id must be a positive integer.")
        process_create_time = _runtime_identifier(
            process_create_time, "process_create_time"
        )
        if not isinstance(command_summary, str) or not command_summary:
            raise ValueError("command_summary must not be empty.")
        if len(command_summary) > 1024 or not command_summary.isprintable():
            raise ValueError("command_summary must be printable and at most 1024 characters.")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                running = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ? AND execution_status = 'running'",
                    (job_id,),
                ).fetchone()
                if running is None:
                    raise OptimisticLockConflictError()
                cursor = connection.execute(
                    """
                    UPDATE job_processes
                    SET pid = ?, process_create_time = ?, process_group_id = ?,
                        command_summary = ?, heartbeat_at = ?, started_at = ?,
                        launch_state = 'registered', launch_error_code = NULL
                    WHERE job_id = ? AND launch_token = ? AND pid IS NULL
                      AND launch_state IN ('claimed', 'identity_unresolved')
                    """,
                    (
                        pid,
                        process_create_time,
                        process_group_id,
                        command_summary,
                        now,
                        now,
                        job_id,
                        launch_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise OptimisticLockConflictError()
                process = connection.execute(
                    "SELECT * FROM job_processes WHERE job_id = ?", (job_id,)
                ).fetchone()
                if process is None:
                    raise OptimisticLockConflictError()
                if running["recovery_status"] == "prepared":
                    next_version = int(running["version"]) + 1
                    connection.execute(
                        """
                        UPDATE jobs
                        SET recovery_status = 'running', version = ?, updated_at = ?
                        WHERE job_id = ? AND version = ?
                        """,
                        (next_version, now, job_id, running["version"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO job_events (
                            job_id, event_type, source, job_version,
                            execution_status, evaluation_status, quality_status, created_at
                        ) VALUES (?, 'job.recovery_started', 'recovery', ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            next_version,
                            running["execution_status"],
                            running["evaluation_status"],
                            running["quality_status"],
                            now,
                        ),
                    )
                connection.commit()
                return dict(process)
            except Exception:
                connection.rollback()
                raise

    def mark_launch_identity_unresolved(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
    ) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        launch_token = _runtime_identifier(launch_token, "launch_token")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                job = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                process = connection.execute(
                    "SELECT * FROM job_processes WHERE job_id = ?", (job_id,)
                ).fetchone()
                if job is None:
                    raise JobNotFoundError()
                if (
                    process is None
                    or job["execution_status"] != "running"
                    or process["launch_token"] != launch_token
                    or process["launch_state"] == "exited"
                ):
                    raise OptimisticLockConflictError()
                identity_incomplete = (
                    process["pid"] is None
                    or not process["process_create_time"]
                    or process["process_group_id"] is None
                )
                if not identity_incomplete:
                    raise OptimisticLockConflictError()
                if process["launch_state"] == "identity_unresolved":
                    connection.commit()
                    return dict(process)

                cursor = connection.execute(
                    """
                    UPDATE job_processes
                    SET launch_state = 'identity_unresolved',
                        launch_error_code = 'process_identity_unresolved',
                        heartbeat_at = ?
                    WHERE job_id = ? AND launch_token = ?
                      AND launch_state != 'exited'
                      AND (
                          pid IS NULL
                          OR NULLIF(process_create_time, '') IS NULL
                          OR process_group_id IS NULL
                      )
                    """,
                    (now, job_id, launch_token),
                )
                if cursor.rowcount != 1:
                    raise OptimisticLockConflictError()
                connection.execute(
                    """
                    UPDATE job_commands
                    SET status = 'failed',
                        error_code = 'process_identity_unresolved',
                        result_code = 'process_identity_unresolved',
                        completed_at = ?, updated_at = ?,
                        version = version + 1
                    WHERE job_id = ? AND command_type = 'cancel'
                      AND status IN ('pending', 'claimed')
                    """,
                    (now, now, job_id),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, event_type, source, job_version,
                        execution_status, evaluation_status, quality_status, created_at
                    ) VALUES (
                        ?, 'job.launch_identity_unresolved', 'worker', ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        job_id,
                        job["version"],
                        job["execution_status"],
                        job["evaluation_status"],
                        job["quality_status"],
                        now,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM job_processes WHERE job_id = ?", (job_id,)
                ).fetchone()
                connection.commit()
                return dict(updated)
            except Exception:
                connection.rollback()
                raise

    def record_stage_checkpoint(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        checkpoint: Mapping[str, object],
        checkpoint_path: str,
    ) -> dict[str, Any]:
        """Persist validated checkpoint metadata without storing artifact contents."""

        job_id = validate_job_id(job_id)
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        launch_token = _runtime_identifier(launch_token, "launch_token")
        checkpoint_path = _checkpoint_path(checkpoint_path)
        checkpoint_version = checkpoint.get("version")
        stage_name = checkpoint.get("stage_name")
        stage_sequence = checkpoint.get("stage_sequence")
        stage_attempt = checkpoint.get("stage_attempt")
        status = checkpoint.get("status")
        resume_from_stage = checkpoint.get("resume_from_stage")
        error_code = checkpoint.get("error_code")
        if (
            not isinstance(checkpoint_version, int)
            or isinstance(checkpoint_version, bool)
            or checkpoint_version < 1
        ):
            raise ValueError("checkpoint version must be a positive integer.")
        if not isinstance(stage_name, str) or stage_name not in CHECKPOINT_STAGES:
            raise ValueError("checkpoint stage is not allowed.")
        if (
            not isinstance(stage_sequence, int)
            or isinstance(stage_sequence, bool)
            or stage_sequence < 1
        ):
            raise ValueError("checkpoint stage_sequence must be a positive integer.")
        if (
            not isinstance(stage_attempt, int)
            or isinstance(stage_attempt, bool)
            or stage_attempt < 1
        ):
            raise ValueError("checkpoint stage_attempt must be a positive integer.")
        if not isinstance(status, str) or status not in CHECKPOINT_STATUSES:
            raise ValueError("checkpoint status is not allowed.")
        if not checkpoint_stage_order_is_valid(stage_name, stage_sequence):
            raise ValueError("checkpoint stage order is invalid.")
        if checkpoint_path != checkpoint_relative_path(checkpoint):
            raise ValueError("checkpoint_path does not match the server-defined key.")
        if resume_from_stage is not None and (
            not isinstance(resume_from_stage, str)
            or resume_from_stage not in CHECKPOINT_STAGES
        ):
            raise ValueError("checkpoint resume stage is not allowed.")
        if error_code is not None:
            error_code = _runtime_identifier(str(error_code), "error_code")
        if status == "running" and (resume_from_stage is not None or error_code is not None):
            raise ValueError("running checkpoint metadata is inconsistent.")
        if status == "completed" and error_code is not None:
            raise ValueError("completed checkpoint metadata is inconsistent.")
        if status == "failed" and (error_code is None or resume_from_stage is not None):
            raise ValueError("failed checkpoint metadata is inconsistent.")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                job = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                process = connection.execute(
                    "SELECT * FROM job_processes WHERE job_id = ?", (job_id,)
                ).fetchone()
                if job is None or process is None:
                    raise JobNotFoundError()
                if (
                    job["execution_status"] != "running"
                    or process["launch_token"] != launch_token
                    or process["launch_state"] not in {"registered", "exited"}
                ):
                    raise OptimisticLockConflictError()
                existing = connection.execute(
                    """
                    SELECT * FROM stage_runs
                    WHERE job_id = ? AND stage_name = ? AND attempt = ?
                    """,
                    (job_id, stage_name, stage_attempt),
                ).fetchone()
                if existing is not None:
                    identity = (
                        existing["stage_sequence"] == stage_sequence
                        and existing["checkpoint_version"] == checkpoint_version
                        and existing["checkpoint_path"] == checkpoint_path
                        and existing["launch_token"] == launch_token
                    )
                    if not identity:
                        raise OptimisticLockConflictError()
                    if existing["status"] == status:
                        connection.commit()
                        return dict(existing)
                    if existing["status"] != "running" or status == "running":
                        raise OptimisticLockConflictError()
                    next_stage_version = int(existing["version"]) + 1
                    connection.execute(
                        """
                        UPDATE stage_runs
                        SET status = ?, version = ?, error_code = ?,
                            resume_from_stage = ?, resume_eligible = ?,
                            finished_at = ?, updated_at = ?
                        WHERE id = ? AND version = ?
                        """,
                        (
                            status,
                            next_stage_version,
                            error_code,
                            resume_from_stage,
                            int(status == "completed" and resume_from_stage is not None),
                            now,
                            now,
                            existing["id"],
                            existing["version"],
                        ),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO stage_runs (
                            job_id, stage_name, attempt, status, version,
                            error_code, started_at, finished_at, created_at, updated_at,
                            stage_sequence, checkpoint_version, checkpoint_path,
                            resume_from_stage, resume_eligible, launch_token
                        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            stage_name,
                            stage_attempt,
                            status,
                            error_code,
                            now,
                            now if status != "running" else None,
                            now,
                            now,
                            stage_sequence,
                            checkpoint_version,
                            checkpoint_path,
                            resume_from_stage,
                            int(status == "completed" and resume_from_stage is not None),
                            launch_token,
                        ),
                    )
                next_job_version = int(job["version"]) + 1
                connection.execute(
                    """
                    UPDATE jobs
                    SET current_stage = ?, current_stage_attempt = ?,
                        last_checkpoint_stage = CASE
                            WHEN ? = 'completed' THEN ? ELSE last_checkpoint_stage END,
                        version = ?, updated_at = ?
                    WHERE job_id = ? AND version = ?
                    """,
                    (
                        stage_name,
                        stage_attempt,
                        status,
                        stage_name,
                        next_job_version,
                        now,
                        job_id,
                        job["version"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, event_type, source, job_version,
                        execution_status, evaluation_status, quality_status, created_at
                    ) VALUES (?, ?, 'pipeline_adapter', ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        f"job.stage_{status}",
                        next_job_version,
                        job["execution_status"],
                        job["evaluation_status"],
                        job["quality_status"],
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM stage_runs
                    WHERE job_id = ? AND stage_name = ? AND attempt = ?
                    """,
                    (job_id, stage_name, stage_attempt),
                ).fetchone()
                connection.commit()
                return dict(row)
            except Exception:
                connection.rollback()
                raise

    def record_completed_checkpoint(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        checkpoint: Mapping[str, object],
        checkpoint_path: str,
    ) -> dict[str, Any]:
        if checkpoint.get("status") != "completed":
            raise ValueError("checkpoint must be completed.")
        return self.record_stage_checkpoint(
            job_id,
            worker_id=worker_id,
            instance_token=instance_token,
            launch_token=launch_token,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
        )

    def list_stage_runs(self, job_id: str) -> list[dict[str, Any]]:
        job_id = validate_job_id(job_id)
        with closing(connect_database(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT * FROM stage_runs
                WHERE job_id = ?
                ORDER BY stage_sequence ASC, attempt ASC, id ASC
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _assert_repair_mutation_allowed(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        now: str,
    ) -> sqlite3.Row:
        self._assert_current_lease(connection, worker_id, instance_token, now)
        job = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        process = connection.execute(
            "SELECT * FROM job_processes WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if job is None or process is None:
            raise JobNotFoundError()
        if (
            job["execution_status"] != "running"
            or process["launch_token"] != launch_token
            or process["launch_state"] not in {"registered", "exited"}
        ):
            raise OptimisticLockConflictError()
        return job

    def _record_repair_attempt(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        attempt: int,
        status: str,
        reason: str,
        result: str | None,
        files_to_fix: list[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        launch_token = _runtime_identifier(launch_token, "launch_token")
        attempt = _repair_attempt(attempt)
        if status not in REPAIR_ATTEMPT_STATUSES:
            raise ValueError("repair attempt status is not allowed.")
        reason = _repair_text(reason, "reason")
        result = _repair_text(result, "result", nullable=True)
        files_json = _repair_files_json(files_to_fix)
        now = utc_now()
        event_type = {
            "running": "job.repair_started",
            "completed": "job.repair_completed",
            "failed": "job.repair_failed",
            "skipped": "job.repair_skipped",
        }[status]
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                job = self._assert_repair_mutation_allowed(
                    connection,
                    job_id=job_id,
                    worker_id=worker_id,
                    instance_token=instance_token,
                    launch_token=launch_token,
                    now=now,
                )
                existing = connection.execute(
                    """
                    SELECT * FROM repair_attempts
                    WHERE job_id = ? AND attempt = ?
                    """,
                    (job_id, attempt),
                ).fetchone()
                if existing is not None:
                    if existing["files_to_fix_json"] != files_json:
                        raise OptimisticLockConflictError()
                    existing_result = existing["result"]
                    existing_status = existing["status"]
                    if existing_status == status and existing_result == result:
                        connection.commit()
                        return _repair_attempt_row_to_dict(existing)
                    if status == "running" and existing_status in {
                        "running",
                        "completed",
                        "failed",
                        "skipped",
                    }:
                        connection.commit()
                        return _repair_attempt_row_to_dict(existing)
                    if existing_status != "running" or status == "running":
                        raise OptimisticLockConflictError()
                    connection.execute(
                        """
                        UPDATE repair_attempts
                        SET status = ?, reason = ?, result = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (status, reason, result, now, existing["id"]),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO repair_attempts (
                            job_id, attempt, status, reason, result,
                            files_to_fix_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            attempt,
                            status,
                            reason,
                            result,
                            files_json,
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, event_type, source, job_version,
                        execution_status, evaluation_status, quality_status, created_at
                    ) VALUES (?, ?, 'worker', ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        event_type,
                        job["version"],
                        job["execution_status"],
                        job["evaluation_status"],
                        job["quality_status"],
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM repair_attempts
                    WHERE job_id = ? AND attempt = ?
                    """,
                    (job_id, attempt),
                ).fetchone()
                connection.commit()
                return _repair_attempt_row_to_dict(row)
            except Exception:
                connection.rollback()
                raise

    def start_repair_attempt(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        attempt: int,
        reason: str,
        files_to_fix: list[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        return self._record_repair_attempt(
            job_id,
            worker_id=worker_id,
            instance_token=instance_token,
            launch_token=launch_token,
            attempt=attempt,
            status="running",
            reason=reason,
            result=None,
            files_to_fix=files_to_fix,
        )

    def complete_repair_attempt(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        attempt: int,
        result: str,
        files_to_fix: list[str] | tuple[str, ...],
        reason: str = "quality_rejected",
    ) -> dict[str, Any]:
        return self._record_repair_attempt(
            job_id,
            worker_id=worker_id,
            instance_token=instance_token,
            launch_token=launch_token,
            attempt=attempt,
            status="completed",
            reason=reason,
            result=result,
            files_to_fix=files_to_fix,
        )

    def fail_repair_attempt(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        attempt: int,
        reason: str,
        result: str,
        files_to_fix: list[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        return self._record_repair_attempt(
            job_id,
            worker_id=worker_id,
            instance_token=instance_token,
            launch_token=launch_token,
            attempt=attempt,
            status="failed",
            reason=reason,
            result=result,
            files_to_fix=files_to_fix,
        )

    def skip_repair_attempt(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        attempt: int,
        reason: str,
        files_to_fix: list[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        return self._record_repair_attempt(
            job_id,
            worker_id=worker_id,
            instance_token=instance_token,
            launch_token=launch_token,
            attempt=attempt,
            status="skipped",
            reason=reason,
            result=None,
            files_to_fix=files_to_fix,
        )

    def list_repair_attempts(self, job_id: str) -> list[dict[str, Any]]:
        job_id = validate_job_id(job_id)
        with closing(connect_database(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT * FROM repair_attempts
                WHERE job_id = ?
                ORDER BY attempt ASC, id ASC
                """,
                (job_id,),
            ).fetchall()
        return [_repair_attempt_row_to_dict(row) for row in rows]

    def prepare_recovery_attempt(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        new_launch_token: str,
        resume_from_stage: str,
        resume_stage_sequence: int,
        resume_stage_attempt: int = 1,
        max_recoveries: int = DEFAULT_MAX_RECOVERIES,
        observed_exit_code: int | None = None,
    ) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        launch_token = _runtime_identifier(launch_token, "launch_token")
        new_launch_token = _runtime_identifier(new_launch_token, "new_launch_token")
        if resume_from_stage not in CHECKPOINT_STAGES:
            raise ValueError("resume_from_stage is not allowed.")
        integer_values = (resume_stage_sequence, resume_stage_attempt, max_recoveries)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in integer_values
        ):
            raise ValueError("Recovery sequence, attempt, and limit must be positive integers.")
        if observed_exit_code is not None and (
            not isinstance(observed_exit_code, int)
            or isinstance(observed_exit_code, bool)
        ):
            raise ValueError("observed_exit_code must be an integer or None.")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                job = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                process = connection.execute(
                    "SELECT * FROM job_processes WHERE job_id = ?", (job_id,)
                ).fetchone()
                if job is None or process is None:
                    raise JobNotFoundError()
                if (
                    process["launch_token"] == new_launch_token
                    and process["launch_state"] == "claimed"
                    and job["execution_status"] == "running"
                    and job["recovery_status"] == "prepared"
                    and job["current_stage"] == resume_from_stage
                    and job["current_stage_attempt"] == resume_stage_attempt
                ):
                    connection.commit()
                    return _row_to_dict(job)
                if (
                    job["execution_status"] != "running"
                    or process["launch_token"] != launch_token
                    or process["launch_state"] != "registered"
                    or process["pid"] is None
                    or not process["process_create_time"]
                    or process["process_group_id"] is None
                ):
                    raise OptimisticLockConflictError()
                if int(job["recovery_count"]) >= max_recoveries:
                    raise OptimisticLockConflictError(
                        details={"reason": "recovery_attempts_exhausted"}
                    )
                pending_cancel = connection.execute(
                    """
                    SELECT 1 FROM job_commands
                    WHERE job_id = ? AND command_type = 'cancel'
                      AND status IN ('pending', 'claimed')
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                if pending_cancel is not None:
                    raise OptimisticLockConflictError(
                        details={"reason": "cancel_requested"}
                    )
                completed = connection.execute(
                    """
                    SELECT 1 FROM stage_runs
                    WHERE job_id = ? AND launch_token = ? AND status = 'completed'
                      AND resume_from_stage = ? AND resume_eligible = 1
                    LIMIT 1
                    """,
                    (job_id, launch_token, resume_from_stage),
                ).fetchone()
                if completed is None:
                    raise OptimisticLockConflictError(
                        details={"reason": "completed_checkpoint_missing"}
                    )
                process_attempt = int(process["process_attempt"])
                connection.execute(
                    """
                    INSERT INTO job_process_history (
                        job_id, process_attempt, worker_id, launch_token, pid,
                        process_create_time, process_group_id, command_summary,
                        heartbeat_at, started_at, exited_at, exit_code,
                        launch_state, launch_error_code, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'exited', 'checkpoint_recovery_replaced', ?)
                    """,
                    (
                        job_id,
                        process_attempt,
                        process["worker_id"],
                        process["launch_token"],
                        process["pid"],
                        process["process_create_time"],
                        process["process_group_id"],
                        process["command_summary"],
                        process["heartbeat_at"],
                        process["started_at"],
                        now,
                        observed_exit_code,
                        now,
                    ),
                )
                cursor = connection.execute(
                    """
                    UPDATE job_processes
                    SET worker_id = ?, launch_token = ?, pid = NULL,
                        process_create_time = NULL, process_group_id = NULL,
                        command_summary = NULL, heartbeat_at = ?, started_at = NULL,
                        exited_at = NULL, exit_code = NULL, launch_state = 'claimed',
                        launch_error_code = NULL, process_attempt = ?
                    WHERE job_id = ? AND launch_token = ? AND launch_state = 'registered'
                    """,
                    (
                        worker_id,
                        new_launch_token,
                        now,
                        process_attempt + 1,
                        job_id,
                        launch_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise OptimisticLockConflictError()
                next_version = int(job["version"]) + 1
                connection.execute(
                    """
                    UPDATE jobs
                    SET recovery_count = recovery_count + 1,
                        recovery_status = 'prepared', recovery_error_code = NULL,
                        current_stage = ?, current_stage_attempt = ?,
                        failure_code = NULL, version = ?, updated_at = ?
                    WHERE job_id = ? AND version = ?
                    """,
                    (
                        resume_from_stage,
                        resume_stage_attempt,
                        next_version,
                        now,
                        job_id,
                        job["version"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, event_type, source, job_version,
                        execution_status, evaluation_status, quality_status, created_at
                    ) VALUES (?, 'job.recovery_prepared', 'recovery', ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        next_version,
                        job["execution_status"],
                        job["evaluation_status"],
                        job["quality_status"],
                        now,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                connection.commit()
                return _row_to_dict(updated)
            except Exception:
                connection.rollback()
                raise

    def heartbeat_process(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
    ) -> bool:
        job_id = validate_job_id(job_id)
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        launch_token = _runtime_identifier(launch_token, "launch_token")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                cursor = connection.execute(
                    """
                    UPDATE job_processes
                    SET heartbeat_at = ?
                    WHERE job_id = ? AND launch_token = ? AND exited_at IS NULL
                    """,
                    (now, job_id, launch_token),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return cursor.rowcount == 1

    def get_process(self, job_id: str) -> dict[str, Any] | None:
        job_id = validate_job_id(job_id)
        with closing(connect_database(self.database_path)) as connection:
            row = connection.execute(
                "SELECT * FROM job_processes WHERE job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_running_processes(self) -> list[dict[str, Any]]:
        with closing(connect_database(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT j.*, p.worker_id, p.launch_token, p.pid,
                       p.process_create_time, p.process_group_id,
                       p.command_summary, p.heartbeat_at, p.started_at,
                       p.exited_at, p.exit_code, p.launch_state,
                       p.launch_error_code
                FROM jobs AS j
                LEFT JOIN job_processes AS p ON p.job_id = j.job_id
                WHERE j.execution_status = 'running'
                ORDER BY j.created_at ASC, j.rowid ASC
                """
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def _finish_running_process(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        execution_status: str,
        event_type: str,
        exit_code: int | None,
        failure_code: str | None,
        evaluation_status: str | None = None,
        quality_status: str | None = None,
        cancel_command_status: str | None = None,
        cancel_command_error_code: str | None = None,
    ) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        worker_id = _runtime_identifier(worker_id, "worker_id")
        instance_token = _runtime_identifier(instance_token, "instance_token")
        launch_token = _runtime_identifier(launch_token, "launch_token")
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_lease(
                    connection, worker_id, instance_token, now
                )
                current = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                process = connection.execute(
                    "SELECT * FROM job_processes WHERE job_id = ?", (job_id,)
                ).fetchone()
                if current is None or process is None:
                    raise JobNotFoundError()
                if process["launch_token"] != launch_token:
                    raise OptimisticLockConflictError()
                if current["execution_status"] != "running":
                    connection.commit()
                    return _row_to_dict(current)
                if execution_status == "canceled":
                    cancel_command = connection.execute(
                        """
                        SELECT status FROM job_commands
                        WHERE job_id = ? AND command_type = 'cancel'
                        ORDER BY id DESC LIMIT 1
                        """,
                        (job_id,),
                    ).fetchone()
                    if (
                        cancel_command is None
                        or cancel_command["status"] not in {"pending", "claimed"}
                    ):
                        raise OptimisticLockConflictError()
                transition_kwargs = {"execution_status": execution_status}
                if evaluation_status is not None:
                    transition_kwargs["evaluation_status"] = evaluation_status
                if quality_status is not None:
                    transition_kwargs["quality_status"] = quality_status
                next_state = validate_job_transition(
                    JobState(
                        execution_status=current["execution_status"],
                        evaluation_status=current["evaluation_status"],
                        quality_status=current["quality_status"],
                    ),
                    **transition_kwargs,
                )
                next_version = int(current["version"]) + 1
                recovery_status = (
                    "completed" if execution_status == "completed" else "failed"
                )
                recovery_error_code = (
                    None
                    if execution_status == "completed"
                    else (failure_code or "recovery_canceled")
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET execution_status = ?, evaluation_status = ?,
                        quality_status = ?, failure_code = ?,
                        recovery_status = CASE
                            WHEN recovery_count > 0 THEN ? ELSE recovery_status END,
                        recovery_error_code = CASE
                            WHEN recovery_count > 0 THEN ? ELSE recovery_error_code END,
                        version = ?, updated_at = ?
                    WHERE job_id = ? AND version = ?
                    """,
                    (
                        next_state.execution_status,
                        next_state.evaluation_status,
                        next_state.quality_status,
                        failure_code,
                        recovery_status,
                        recovery_error_code,
                        next_version,
                        now,
                        job_id,
                        current["version"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE job_processes
                    SET heartbeat_at = ?, exited_at = ?, exit_code = ?,
                        launch_state = 'exited'
                    WHERE job_id = ? AND launch_token = ?
                    """,
                    (now, now, exit_code, job_id, launch_token),
                )
                if cancel_command_status is not None:
                    connection.execute(
                        """
                        UPDATE job_commands
                        SET status = ?, error_code = ?, completed_at = ?,
                            result_code = ?,
                            updated_at = ?,
                            version = version + 1
                        WHERE job_id = ? AND command_type = 'cancel'
                          AND status IN ('pending', 'claimed')
                        """,
                        (
                            cancel_command_status,
                            (
                                cancel_command_error_code
                                if cancel_command_status == "failed"
                                else None
                            ),
                            now,
                            (
                                cancel_command_error_code
                                if cancel_command_status == "failed"
                                else "canceled"
                            ),
                            now,
                            job_id,
                        ),
                    )
                elif execution_status != "canceled":
                    connection.execute(
                        """
                        UPDATE job_commands
                        SET status = 'failed', error_code = 'already_finished',
                            result_code = 'already_finished',
                            completed_at = ?, updated_at = ?,
                            version = version + 1
                        WHERE job_id = ? AND command_type = 'cancel'
                          AND status IN ('pending', 'claimed')
                        """,
                        (now, now, job_id),
                    )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, event_type, source, job_version,
                        execution_status, evaluation_status, quality_status, created_at
                    ) VALUES (?, ?, 'worker', ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        event_type,
                        next_version,
                        next_state.execution_status,
                        next_state.evaluation_status,
                        next_state.quality_status,
                        now,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                connection.commit()
                return _row_to_dict(updated)
            except Exception:
                connection.rollback()
                raise

    def finish_process(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        exit_code: int,
        evaluation_status: str | None = None,
        quality_status: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ValueError("exit_code must be an integer.")
        if exit_code == 0:
            return self._finish_running_process(
                job_id,
                worker_id=worker_id,
                instance_token=instance_token,
                launch_token=launch_token,
                execution_status="completed",
                event_type="job.process_completed",
                exit_code=exit_code,
                failure_code=None,
                evaluation_status=evaluation_status,
                quality_status=quality_status,
            )
        return self._finish_running_process(
            job_id,
            worker_id=worker_id,
            instance_token=instance_token,
            launch_token=launch_token,
            execution_status="failed",
            event_type="job.process_failed",
            exit_code=exit_code,
            failure_code="pipeline_process_failed",
            evaluation_status=evaluation_status or "skipped",
            quality_status=quality_status or "skipped",
        )

    def complete_cancellation(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        exit_code: int | None,
    ) -> dict[str, Any]:
        return self._finish_running_process(
            job_id,
            worker_id=worker_id,
            instance_token=instance_token,
            launch_token=launch_token,
            execution_status="canceled",
            event_type="job.canceled",
            exit_code=exit_code,
            failure_code=None,
            cancel_command_status="completed",
        )

    def fail_process(
        self,
        job_id: str,
        *,
        worker_id: str,
        instance_token: str,
        launch_token: str,
        failure_code: str,
        event_type: str = "job.process_failed",
        exit_code: int | None = None,
        cancel_command_error_code: str | None = None,
    ) -> dict[str, Any]:
        failure_code = _runtime_identifier(failure_code, "failure_code")
        event_type = _runtime_identifier(event_type, "event_type")
        if cancel_command_error_code is not None:
            cancel_command_error_code = _runtime_identifier(
                cancel_command_error_code,
                "cancel_command_error_code",
            )
        return self._finish_running_process(
            job_id,
            worker_id=worker_id,
            instance_token=instance_token,
            launch_token=launch_token,
            execution_status="failed",
            event_type=event_type,
            exit_code=exit_code,
            failure_code=failure_code,
            evaluation_status="skipped",
            quality_status="skipped",
            cancel_command_status="failed",
            cancel_command_error_code=cancel_command_error_code or failure_code,
        )

    def transition_job(
        self,
        job_id: str,
        *,
        expected_version: int,
        execution_status: str | None = None,
        evaluation_status: str | None = None,
        quality_status: str | None = None,
        source: str = "control_plane",
    ) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError("expected_version must be an integer.")
        if source not in EVENT_SOURCES:
            raise ValueError("source is not an allowed job event source.")

        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if current is None:
                    raise JobNotFoundError()
                if current["version"] != expected_version:
                    raise OptimisticLockConflictError(
                        details={
                            "expected_version": expected_version,
                            "actual_version": current["version"],
                        }
                    )
                next_state = validate_job_transition(
                    JobState(
                        execution_status=current["execution_status"],
                        evaluation_status=current["evaluation_status"],
                        quality_status=current["quality_status"],
                    ),
                    execution_status=execution_status,
                    evaluation_status=evaluation_status,
                    quality_status=quality_status,
                )
                next_version = expected_version + 1
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE jobs
                    SET execution_status = ?, evaluation_status = ?, quality_status = ?,
                        version = ?, updated_at = ?
                    WHERE job_id = ? AND version = ?
                    """,
                    (
                        next_state.execution_status,
                        next_state.evaluation_status,
                        next_state.quality_status,
                        next_version,
                        now,
                        job_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise OptimisticLockConflictError()
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, event_type, source, job_version,
                        execution_status, evaluation_status, quality_status, created_at
                    ) VALUES (?, 'job.status_changed', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        source,
                        next_version,
                        next_state.execution_status,
                        next_state.evaluation_status,
                        next_state.quality_status,
                        now,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                connection.commit()
                return _row_to_dict(updated)
            except Exception:
                connection.rollback()
                raise

    def cost_summary(self, job_id: str) -> dict[str, Any]:
        from codes.cost_ledger import summarize_job_costs

        return summarize_job_costs(self.database_path, validate_job_id(job_id))

    def add_cost_entry(
        self,
        job_id: str,
        *,
        provider: str,
        model: str,
        category: str,
        amount_microusd: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        stage_run_id: int | None = None,
    ) -> int:
        integer_values = (amount_microusd, input_tokens, output_tokens)
        if any(not isinstance(value, int) or isinstance(value, bool) for value in integer_values):
            raise ValueError("Cost and token values must be integers.")
        if any(value < 0 for value in integer_values):
            raise ValueError("Cost and token values must not be negative.")
        with closing(connect_database(self.database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO cost_entries (
                        job_id, stage_run_id, provider, model, category,
                        amount_microusd, input_tokens, output_tokens, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        stage_run_id,
                        provider,
                        model,
                        category,
                        amount_microusd,
                        input_tokens,
                        output_tokens,
                        utc_now(),
                    ),
                )
                return int(cursor.lastrowid)
