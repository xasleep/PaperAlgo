from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from .database import connect_database, initialize_database, utc_now
from .errors import (
    IdempotencyConflictError,
    InvalidParameterError,
    JobNotFoundError,
    OptimisticLockConflictError,
)
from .job_state import JobState, validate_job_transition
from .path_security import validate_job_id


REQUEST_FIELDS = frozenset(
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
EVENT_SOURCES = frozenset({"control_plane", "pipeline_adapter", "recovery", "worker"})


def _canonical_request(request: Mapping[str, object]) -> tuple[dict[str, object], str]:
    if set(request) != REQUEST_FIELDS:
        raise ValueError("SQLite job requests must contain only the validated job fields.")
    normalized = {field: request[field] for field in sorted(REQUEST_FIELDS)}
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


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for field in ("auto_refine", "skip_mineru"):
        if field in result:
            result[field] = bool(result[field])
    result.pop("idempotency_key_hash", None)
    result.pop("request_hash", None)
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

                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, idempotency_key_hash, request_hash, paper_name,
                        upload_id, domain, eval_type, generated_n, auto_refine,
                        max_repair_rounds, console_output, skip_mineru,
                        pdf_markdown_path, execution_status, evaluation_status,
                        quality_status, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
