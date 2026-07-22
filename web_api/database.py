from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from .config import configured_database_path


SQLITE_BUSY_TIMEOUT_MS = 5000
_INITIALIZATION_LOCKS_GUARD = Lock()
_INITIALIZATION_LOCKS: dict[Path, Lock] = {}


SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                idempotency_key_hash TEXT UNIQUE,
                request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
                paper_name TEXT NOT NULL,
                upload_id TEXT NOT NULL,
                domain TEXT NOT NULL,
                eval_type TEXT NOT NULL,
                generated_n INTEGER NOT NULL
                    CHECK(typeof(generated_n) = 'integer' AND generated_n > 0),
                auto_refine INTEGER NOT NULL CHECK(auto_refine IN (0, 1)),
                max_repair_rounds INTEGER NOT NULL
                    CHECK(typeof(max_repair_rounds) = 'integer' AND max_repair_rounds >= 0),
                console_output TEXT NOT NULL,
                skip_mineru INTEGER NOT NULL CHECK(skip_mineru IN (0, 1)),
                pdf_markdown_path TEXT NOT NULL DEFAULT '',
                execution_status TEXT NOT NULL
                    CHECK(execution_status IN ('queued', 'running', 'completed', 'failed', 'canceled')),
                evaluation_status TEXT NOT NULL
                    CHECK(evaluation_status IN ('pending', 'running', 'passed', 'failed', 'skipped')),
                quality_status TEXT NOT NULL
                    CHECK(quality_status IN ('pending', 'assessing', 'accepted', 'rejected', 'skipped')),
                version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX jobs_updated_at_idx ON jobs(updated_at DESC, job_id DESC)",
            """
            CREATE TABLE stage_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                stage_name TEXT NOT NULL,
                attempt INTEGER NOT NULL
                    CHECK(typeof(attempt) = 'integer' AND attempt >= 1),
                status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
                    CHECK(typeof(version) = 'integer' AND version >= 1),
                error_code TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(job_id, stage_name, attempt)
            )
            """,
            """
            CREATE TABLE job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                job_version INTEGER NOT NULL
                    CHECK(typeof(job_version) = 'integer' AND job_version >= 1),
                execution_status TEXT,
                evaluation_status TEXT,
                quality_status TEXT,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX job_events_job_idx ON job_events(job_id, id)",
            """
            CREATE TABLE job_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                command_type TEXT NOT NULL,
                status TEXT NOT NULL,
                dedupe_key_hash TEXT,
                version INTEGER NOT NULL DEFAULT 1
                    CHECK(typeof(version) = 'integer' AND version >= 1),
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                completed_at TEXT,
                UNIQUE(job_id, dedupe_key_hash)
            )
            """,
            """
            CREATE TABLE cost_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                stage_run_id INTEGER REFERENCES stage_runs(id) ON DELETE SET NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                category TEXT NOT NULL,
                amount_microusd INTEGER NOT NULL
                    CHECK(typeof(amount_microusd) = 'integer' AND amount_microusd >= 0),
                input_tokens INTEGER NOT NULL DEFAULT 0
                    CHECK(typeof(input_tokens) = 'integer' AND input_tokens >= 0),
                output_tokens INTEGER NOT NULL DEFAULT 0
                    CHECK(typeof(output_tokens) = 'integer' AND output_tokens >= 0),
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX cost_entries_job_idx ON cost_entries(job_id, id)",
            """
            CREATE TABLE worker_leases (
                lease_name TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
                    CHECK(typeof(version) = 'integer' AND version >= 1),
                acquired_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    (
        2,
        (
            "ALTER TABLE jobs ADD COLUMN failure_code TEXT",
            "ALTER TABLE job_commands ADD COLUMN claimed_by_worker_id TEXT",
            "ALTER TABLE job_commands ADD COLUMN error_code TEXT",
            """
            CREATE TABLE job_processes (
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                worker_id TEXT NOT NULL,
                launch_token TEXT NOT NULL UNIQUE,
                pid INTEGER
                    CHECK(pid IS NULL OR (typeof(pid) = 'integer' AND pid > 0)),
                process_create_time TEXT,
                process_group_id INTEGER
                    CHECK(process_group_id IS NULL OR (
                        typeof(process_group_id) = 'integer' AND process_group_id > 0
                    )),
                command_summary TEXT
                    CHECK(command_summary IS NULL OR length(command_summary) <= 1024),
                heartbeat_at TEXT NOT NULL,
                started_at TEXT,
                exited_at TEXT,
                exit_code INTEGER
                    CHECK(exit_code IS NULL OR typeof(exit_code) = 'integer')
            )
            """,
            "CREATE INDEX job_processes_worker_idx ON job_processes(worker_id, heartbeat_at)",
            """
            CREATE INDEX job_commands_pending_idx
            ON job_commands(command_type, status, id)
            """,
        ),
    ),
    (
        3,
        (
            "ALTER TABLE worker_leases ADD COLUMN owner_token TEXT",
        ),
    ),
)


def normalize_database_path(path: Path | str | None = None) -> Path:
    database_path = Path(path) if path is not None else configured_database_path()
    return Path(os.path.abspath(os.fspath(database_path.expanduser())))


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")


def _remove_new_database_files(database_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{database_path}{suffix}")
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue


def connect_database(path: Path | str | None = None) -> sqlite3.Connection:
    database_path = normalize_database_path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    existed_before = database_path.exists()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            database_path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        )
        _configure_connection(connection)
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        if not existed_before:
            _remove_new_database_files(database_path)
        raise


def _initialization_lock(database_path: Path) -> Lock:
    with _INITIALIZATION_LOCKS_GUARD:
        lock = _INITIALIZATION_LOCKS.get(database_path)
        if lock is None:
            lock = Lock()
            _INITIALIZATION_LOCKS[database_path] = lock
        return lock


def _enable_wal(connection: sqlite3.Connection) -> None:
    journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(journal_mode).lower() != "wal":
        raise sqlite3.OperationalError("SQLite WAL mode could not be enabled.")


def _ensure_schema_migrations_table(connection: sqlite3.Connection) -> None:
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("schema_migrations",),
    ).fetchone()
    if existing is None:
        connection.execute(SCHEMA_MIGRATIONS_SQL)


def initialize_database(path: Path | str | None = None) -> Path:
    database_path = normalize_database_path(path)
    with _initialization_lock(database_path):
        existed_before = database_path.exists()
        connection = connect_database(database_path)
        try:
            _enable_wal(connection)
        except Exception:
            connection.close()
            if not existed_before:
                _remove_new_database_files(database_path)
            raise
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _ensure_schema_migrations_table(connection)
                applied = {
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    )
                }
                for version, statements in MIGRATIONS:
                    if version in applied:
                        continue
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, utc_now()),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()
    return database_path
