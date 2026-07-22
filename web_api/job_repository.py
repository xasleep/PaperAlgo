from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
GLOBAL_WORKER_LEASE = "pipeline-worker"
CANCEL_DEDUPE_HASH = hashlib.sha256(b"cancel:v1").hexdigest()


def _future_utc(seconds: float) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _runtime_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{field} must contain between 1 and 256 characters.")
    if not value.isprintable():
        raise ValueError(f"{field} must contain printable characters.")
    return value


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
                        job_id, worker_id, launch_token, heartbeat_at
                    ) VALUES (?, ?, ?, ?)
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

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        now = utc_now()
        with closing(connect_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                job = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if job is None:
                    raise JobNotFoundError()
                if job["execution_status"] in {"completed", "failed"}:
                    raise JobNotCancelableError(
                        "already_finished",
                        "Job is already finished and cannot be canceled.",
                    )
                command = connection.execute(
                    """
                    SELECT * FROM job_commands
                    WHERE job_id = ? AND command_type = 'cancel'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                if command is None and job["execution_status"] != "canceled":
                    connection.execute(
                        """
                        INSERT INTO job_commands (
                            job_id, command_type, status, dedupe_key_hash,
                            version, created_at
                        ) VALUES (?, 'cancel', 'pending', ?, 1, ?)
                        """,
                        (job_id, CANCEL_DEDUPE_HASH, now),
                    )
                    command = connection.execute(
                        """
                        SELECT * FROM job_commands
                        WHERE job_id = ? AND command_type = 'cancel'
                        """,
                        (job_id,),
                    ).fetchone()
                connection.commit()
                if command is None:
                    return {
                        "job_id": job_id,
                        "command_type": "cancel",
                        "status": "completed",
                    }
                return dict(command)
            except Exception:
                connection.rollback()
                raise

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
        return dict(row) if row is not None else None

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
                        version = version + 1
                    WHERE id = ?
                    """,
                    (worker_id, now, now, row["command_id"]),
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
                            claimed_at = COALESCE(claimed_at, ?), version = version + 1
                        WHERE id = ?
                        """,
                        (worker_id, now, command["id"]),
                    )
                    command = connection.execute(
                        "SELECT * FROM job_commands WHERE id = ?", (command["id"],)
                    ).fetchone()
                connection.commit()
                return dict(command)
            except Exception:
                connection.rollback()
                raise

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
                    "SELECT 1 FROM jobs WHERE job_id = ? AND execution_status = 'running'",
                    (job_id,),
                ).fetchone()
                if running is None:
                    raise OptimisticLockConflictError()
                cursor = connection.execute(
                    """
                    UPDATE job_processes
                    SET pid = ?, process_create_time = ?, process_group_id = ?,
                        command_summary = ?, heartbeat_at = ?, started_at = ?
                    WHERE job_id = ? AND launch_token = ? AND pid IS NULL
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
                connection.commit()
                return dict(process)
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
                       p.exited_at, p.exit_code
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
        cancel_command_status: str | None = None,
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
                next_state = validate_job_transition(
                    JobState(
                        execution_status=current["execution_status"],
                        evaluation_status=current["evaluation_status"],
                        quality_status=current["quality_status"],
                    ),
                    execution_status=execution_status,
                )
                next_version = int(current["version"]) + 1
                connection.execute(
                    """
                    UPDATE jobs
                    SET execution_status = ?, evaluation_status = ?,
                        quality_status = ?, failure_code = ?, version = ?, updated_at = ?
                    WHERE job_id = ? AND version = ?
                    """,
                    (
                        next_state.execution_status,
                        next_state.evaluation_status,
                        next_state.quality_status,
                        failure_code,
                        next_version,
                        now,
                        job_id,
                        current["version"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE job_processes
                    SET heartbeat_at = ?, exited_at = ?, exit_code = ?
                    WHERE job_id = ? AND launch_token = ?
                    """,
                    (now, now, exit_code, job_id, launch_token),
                )
                if cancel_command_status is not None:
                    connection.execute(
                        """
                        UPDATE job_commands
                        SET status = ?, error_code = ?, completed_at = ?,
                            version = version + 1
                        WHERE job_id = ? AND command_type = 'cancel'
                          AND status IN ('pending', 'claimed')
                        """,
                        (
                            cancel_command_status,
                            failure_code if cancel_command_status == "failed" else None,
                            now,
                            job_id,
                        ),
                    )
                elif execution_status != "canceled":
                    connection.execute(
                        """
                        UPDATE job_commands
                        SET status = 'failed', error_code = 'already_finished',
                            completed_at = ?, version = version + 1
                        WHERE job_id = ? AND command_type = 'cancel'
                          AND status IN ('pending', 'claimed')
                        """,
                        (now, job_id),
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
    ) -> dict[str, Any]:
        failure_code = _runtime_identifier(failure_code, "failure_code")
        event_type = _runtime_identifier(event_type, "event_type")
        return self._finish_running_process(
            job_id,
            worker_id=worker_id,
            instance_token=instance_token,
            launch_token=launch_token,
            execution_status="failed",
            event_type=event_type,
            exit_code=exit_code,
            failure_code=failure_code,
            cancel_command_status="failed",
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
