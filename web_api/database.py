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
                    CHECK(evaluation_status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
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
    (
        4,
        (
            """
            ALTER TABLE job_processes ADD COLUMN launch_state TEXT NOT NULL
                DEFAULT 'claimed'
                CHECK(launch_state IN (
                    'claimed', 'registered', 'identity_unresolved', 'exited'
                ))
            """,
            "ALTER TABLE job_processes ADD COLUMN launch_error_code TEXT",
            """
            UPDATE job_processes
            SET launch_state = 'registered'
            WHERE pid IS NOT NULL AND exited_at IS NULL
            """,
            """
            UPDATE job_processes
            SET launch_state = 'exited'
            WHERE exited_at IS NOT NULL
            """,
        ),
    ),
    (
        5,
        (
            """
            ALTER TABLE jobs ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(recovery_count) = 'integer' AND recovery_count >= 0)
            """,
            """
            ALTER TABLE jobs ADD COLUMN recovery_status TEXT NOT NULL DEFAULT 'none'
                CHECK(recovery_status IN ('none', 'prepared', 'running', 'completed', 'failed'))
            """,
            "ALTER TABLE jobs ADD COLUMN recovery_error_code TEXT",
            "ALTER TABLE jobs ADD COLUMN current_stage TEXT",
            """
            ALTER TABLE jobs ADD COLUMN current_stage_attempt INTEGER
                CHECK(current_stage_attempt IS NULL OR (
                    typeof(current_stage_attempt) = 'integer' AND current_stage_attempt >= 1
                ))
            """,
            "ALTER TABLE jobs ADD COLUMN last_checkpoint_stage TEXT",
            """
            ALTER TABLE job_processes ADD COLUMN process_attempt INTEGER NOT NULL DEFAULT 1
                CHECK(typeof(process_attempt) = 'integer' AND process_attempt >= 1)
            """,
            """
            ALTER TABLE stage_runs ADD COLUMN stage_sequence INTEGER
                CHECK(stage_sequence IS NULL OR (
                    typeof(stage_sequence) = 'integer' AND stage_sequence >= 1
                ))
            """,
            """
            ALTER TABLE stage_runs ADD COLUMN checkpoint_version INTEGER
                CHECK(checkpoint_version IS NULL OR (
                    typeof(checkpoint_version) = 'integer' AND checkpoint_version >= 1
                ))
            """,
            """
            ALTER TABLE stage_runs ADD COLUMN checkpoint_path TEXT
                CHECK(checkpoint_path IS NULL OR length(checkpoint_path) <= 512)
            """,
            "ALTER TABLE stage_runs ADD COLUMN resume_from_stage TEXT",
            """
            ALTER TABLE stage_runs ADD COLUMN resume_eligible INTEGER NOT NULL DEFAULT 0
                CHECK(resume_eligible IN (0, 1))
            """,
            "ALTER TABLE stage_runs ADD COLUMN launch_token TEXT",
            """
            CREATE INDEX stage_runs_sequence_idx
            ON stage_runs(job_id, stage_sequence, attempt)
            """,
            """
            CREATE TRIGGER stage_runs_pr04b_insert_guard
            BEFORE INSERT ON stage_runs
            WHEN NEW.stage_name NOT IN (
                'mineru_parse', 'mineru_skipped', 'planning', 'extract_config',
                'analyzing', 'coding', 'evaluation', 'repair', 'completed'
            ) OR NEW.status NOT IN ('running', 'completed', 'failed')
            BEGIN
                SELECT RAISE(ABORT, 'invalid stage_runs value');
            END
            """,
            """
            CREATE TRIGGER stage_runs_pr04b_update_guard
            BEFORE UPDATE OF stage_name, status ON stage_runs
            WHEN NEW.stage_name NOT IN (
                'mineru_parse', 'mineru_skipped', 'planning', 'extract_config',
                'analyzing', 'coding', 'evaluation', 'repair', 'completed'
            ) OR NEW.status NOT IN ('running', 'completed', 'failed')
            BEGIN
                SELECT RAISE(ABORT, 'invalid stage_runs value');
            END
            """,
            """
            CREATE TABLE job_process_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                process_attempt INTEGER NOT NULL
                    CHECK(typeof(process_attempt) = 'integer' AND process_attempt >= 1),
                worker_id TEXT NOT NULL,
                launch_token TEXT NOT NULL UNIQUE,
                pid INTEGER NOT NULL
                    CHECK(typeof(pid) = 'integer' AND pid > 0),
                process_create_time TEXT NOT NULL,
                process_group_id INTEGER NOT NULL
                    CHECK(typeof(process_group_id) = 'integer' AND process_group_id > 0),
                command_summary TEXT
                    CHECK(command_summary IS NULL OR length(command_summary) <= 1024),
                heartbeat_at TEXT NOT NULL,
                started_at TEXT,
                exited_at TEXT NOT NULL,
                exit_code INTEGER
                    CHECK(exit_code IS NULL OR typeof(exit_code) = 'integer'),
                launch_state TEXT NOT NULL CHECK(launch_state = 'exited'),
                launch_error_code TEXT,
                archived_at TEXT NOT NULL,
                UNIQUE(job_id, process_attempt)
            )
            """,
            """
            CREATE INDEX job_process_history_job_idx
            ON job_process_history(job_id, process_attempt)
            """,
        ),
    ),
    (
        6,
        (
            """
            ALTER TABLE jobs ADD COLUMN reproduce_provider TEXT
                CHECK(reproduce_provider IS NULL OR (
                    typeof(reproduce_provider) = 'text'
                    AND length(reproduce_provider) BETWEEN 1 AND 128
                ))
            """,
            """
            ALTER TABLE jobs ADD COLUMN reproduce_model TEXT
                CHECK(reproduce_model IS NULL OR (
                    typeof(reproduce_model) = 'text'
                    AND length(reproduce_model) BETWEEN 1 AND 128
                ))
            """,
            """
            ALTER TABLE jobs ADD COLUMN evaluation_provider TEXT
                CHECK(evaluation_provider IS NULL OR (
                    typeof(evaluation_provider) = 'text'
                    AND length(evaluation_provider) BETWEEN 1 AND 128
                ))
            """,
            """
            ALTER TABLE jobs ADD COLUMN evaluation_model TEXT
                CHECK(evaluation_model IS NULL OR (
                    typeof(evaluation_model) = 'text'
                    AND length(evaluation_model) BETWEEN 1 AND 128
                ))
            """,
            """
            ALTER TABLE jobs ADD COLUMN evaluation_fallback_models_json TEXT
                CHECK(evaluation_fallback_models_json IS NULL OR (
                    typeof(evaluation_fallback_models_json) = 'text'
                    AND length(evaluation_fallback_models_json) BETWEEN 2 AND 8192
                ))
            """,
            """
            ALTER TABLE jobs ADD COLUMN provider_registry_version INTEGER
                CHECK(provider_registry_version IS NULL OR (
                    typeof(provider_registry_version) = 'integer'
                    AND provider_registry_version >= 1
                ))
            """,
            """
            ALTER TABLE jobs ADD COLUMN provider_contract_fingerprint TEXT
                CHECK(provider_contract_fingerprint IS NULL OR (
                    typeof(provider_contract_fingerprint) = 'text'
                    AND length(provider_contract_fingerprint) = 64
                    AND provider_contract_fingerprint NOT GLOB '*[^0-9a-f]*'
                ))
            """,
        ),
    ),
    (
        7,
        (
            """
            CREATE TABLE jobs_pr06a (
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
                    CHECK(evaluation_status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
                quality_status TEXT NOT NULL
                    CHECK(quality_status IN ('pending', 'assessing', 'accepted', 'rejected', 'skipped')),
                version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                failure_code TEXT,
                recovery_count INTEGER NOT NULL DEFAULT 0
                    CHECK(typeof(recovery_count) = 'integer' AND recovery_count >= 0),
                recovery_status TEXT NOT NULL DEFAULT 'none'
                    CHECK(recovery_status IN ('none', 'prepared', 'running', 'completed', 'failed')),
                recovery_error_code TEXT,
                current_stage TEXT,
                current_stage_attempt INTEGER
                    CHECK(current_stage_attempt IS NULL OR (
                        typeof(current_stage_attempt) = 'integer' AND current_stage_attempt >= 1
                    )),
                last_checkpoint_stage TEXT,
                reproduce_provider TEXT
                    CHECK(reproduce_provider IS NULL OR (
                        typeof(reproduce_provider) = 'text'
                        AND length(reproduce_provider) BETWEEN 1 AND 128
                    )),
                reproduce_model TEXT
                    CHECK(reproduce_model IS NULL OR (
                        typeof(reproduce_model) = 'text'
                        AND length(reproduce_model) BETWEEN 1 AND 128
                    )),
                evaluation_provider TEXT
                    CHECK(evaluation_provider IS NULL OR (
                        typeof(evaluation_provider) = 'text'
                        AND length(evaluation_provider) BETWEEN 1 AND 128
                    )),
                evaluation_model TEXT
                    CHECK(evaluation_model IS NULL OR (
                        typeof(evaluation_model) = 'text'
                        AND length(evaluation_model) BETWEEN 1 AND 128
                    )),
                evaluation_fallback_models_json TEXT
                    CHECK(evaluation_fallback_models_json IS NULL OR (
                        typeof(evaluation_fallback_models_json) = 'text'
                        AND length(evaluation_fallback_models_json) BETWEEN 2 AND 8192
                    )),
                provider_registry_version INTEGER
                    CHECK(provider_registry_version IS NULL OR (
                        typeof(provider_registry_version) = 'integer'
                        AND provider_registry_version >= 1
                    )),
                provider_contract_fingerprint TEXT
                    CHECK(provider_contract_fingerprint IS NULL OR (
                        typeof(provider_contract_fingerprint) = 'text'
                        AND length(provider_contract_fingerprint) = 64
                        AND provider_contract_fingerprint NOT GLOB '*[^0-9a-f]*'
                    ))
            )
            """,
            """
            INSERT INTO jobs_pr06a (
                job_id, idempotency_key_hash, request_hash, paper_name,
                upload_id, domain, eval_type, generated_n, auto_refine,
                max_repair_rounds, console_output, skip_mineru,
                pdf_markdown_path, execution_status, evaluation_status,
                quality_status, version, created_at, updated_at,
                failure_code, recovery_count, recovery_status, recovery_error_code,
                current_stage, current_stage_attempt, last_checkpoint_stage,
                reproduce_provider, reproduce_model, evaluation_provider,
                evaluation_model, evaluation_fallback_models_json,
                provider_registry_version, provider_contract_fingerprint
            )
            SELECT
                job_id, idempotency_key_hash, request_hash, paper_name,
                upload_id, domain, eval_type, generated_n, auto_refine,
                max_repair_rounds, console_output, skip_mineru,
                pdf_markdown_path, execution_status,
                CASE
                    WHEN evaluation_status IN ('passed', 'failed') THEN 'completed'
                    ELSE evaluation_status
                END,
                CASE
                    WHEN evaluation_status = 'passed' AND quality_status = 'pending' THEN 'accepted'
                    WHEN evaluation_status = 'failed' AND quality_status = 'pending' THEN 'rejected'
                    ELSE quality_status
                END,
                version, created_at, updated_at,
                failure_code, recovery_count, recovery_status, recovery_error_code,
                current_stage, current_stage_attempt, last_checkpoint_stage,
                reproduce_provider, reproduce_model, evaluation_provider,
                evaluation_model, evaluation_fallback_models_json,
                provider_registry_version, provider_contract_fingerprint
            FROM jobs
            """,
            "DROP TABLE jobs",
            "ALTER TABLE jobs_pr06a RENAME TO jobs",
            "CREATE INDEX jobs_updated_at_idx ON jobs(updated_at DESC, job_id DESC)",
            """
            CREATE TABLE repair_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                attempt INTEGER NOT NULL
                    CHECK(typeof(attempt) = 'integer' AND attempt >= 1),
                status TEXT NOT NULL
                    CHECK(status IN ('running', 'completed', 'failed', 'skipped')),
                reason TEXT NOT NULL CHECK(length(reason) <= 128),
                result TEXT CHECK(result IS NULL OR length(result) <= 128),
                files_to_fix_json TEXT NOT NULL
                    CHECK(typeof(files_to_fix_json) = 'text' AND length(files_to_fix_json) <= 8192),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(job_id, attempt)
            )
            """,
        ),
    ),
    (
        8,
        (
            """
            ALTER TABLE jobs ADD COLUMN cost_budget_policy TEXT NOT NULL DEFAULT 'none'
                CHECK(cost_budget_policy IN ('none', 'hard'))
            """,
            """
            ALTER TABLE jobs ADD COLUMN cost_budget_currency TEXT
                CHECK(cost_budget_currency IS NULL OR (
                    typeof(cost_budget_currency) = 'text'
                    AND length(cost_budget_currency) BETWEEN 1 AND 16
                ))
            """,
            """
            ALTER TABLE jobs ADD COLUMN cost_budget_amount TEXT
                CHECK(cost_budget_amount IS NULL OR (
                    typeof(cost_budget_amount) = 'text'
                    AND length(cost_budget_amount) BETWEEN 1 AND 64
                ))
            """,
            """
            CREATE TABLE remote_call_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                logical_call_id TEXT NOT NULL
                    CHECK(typeof(logical_call_id) = 'text' AND length(logical_call_id) BETWEEN 1 AND 128),
                attempt_id TEXT NOT NULL
                    CHECK(typeof(attempt_id) = 'text' AND length(attempt_id) BETWEEN 1 AND 128),
                stage TEXT NOT NULL
                    CHECK(typeof(stage) = 'text' AND length(stage) BETWEEN 1 AND 128),
                stage_attempt INTEGER NOT NULL
                    CHECK(typeof(stage_attempt) = 'integer' AND stage_attempt >= 1),
                repair_attempt INTEGER
                    CHECK(repair_attempt IS NULL OR (
                        typeof(repair_attempt) = 'integer' AND repair_attempt >= 1
                    )),
                recovery_attempt INTEGER NOT NULL DEFAULT 0
                    CHECK(typeof(recovery_attempt) = 'integer' AND recovery_attempt >= 0),
                provider_id TEXT NOT NULL
                    CHECK(typeof(provider_id) = 'text' AND length(provider_id) BETWEEN 1 AND 128),
                model_id TEXT NOT NULL
                    CHECK(typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128),
                request_sequence INTEGER NOT NULL
                    CHECK(typeof(request_sequence) = 'integer' AND request_sequence >= 1),
                retry_sequence INTEGER NOT NULL DEFAULT 0
                    CHECK(typeof(retry_sequence) = 'integer' AND retry_sequence >= 0),
                fallback_sequence INTEGER NOT NULL DEFAULT 0
                    CHECK(typeof(fallback_sequence) = 'integer' AND fallback_sequence >= 0),
                pricing_contract_version TEXT NOT NULL
                    CHECK(typeof(pricing_contract_version) = 'text' AND length(pricing_contract_version) BETWEEN 1 AND 64),
                pricing_contract_fingerprint TEXT NOT NULL
                    CHECK(
                        typeof(pricing_contract_fingerprint) = 'text'
                        AND length(pricing_contract_fingerprint) = 64
                        AND pricing_contract_fingerprint NOT GLOB '*[^0-9a-f]*'
                    ),
                pricing_status TEXT NOT NULL
                    CHECK(pricing_status IN ('configured', 'unknown')),
                currency TEXT
                    CHECK(currency IS NULL OR (
                        typeof(currency) = 'text' AND length(currency) BETWEEN 1 AND 16
                    )),
                cost_status TEXT NOT NULL
                    CHECK(cost_status IN ('actual', 'estimated', 'unknown')),
                cost_amount TEXT
                    CHECK(cost_amount IS NULL OR (
                        typeof(cost_amount) = 'text' AND length(cost_amount) BETWEEN 1 AND 128
                    )),
                input_tokens INTEGER
                    CHECK(input_tokens IS NULL OR (
                        typeof(input_tokens) = 'integer' AND input_tokens >= 0
                    )),
                output_tokens INTEGER
                    CHECK(output_tokens IS NULL OR (
                        typeof(output_tokens) = 'integer' AND output_tokens >= 0
                    )),
                cached_input_tokens INTEGER
                    CHECK(cached_input_tokens IS NULL OR (
                        typeof(cached_input_tokens) = 'integer' AND cached_input_tokens >= 0
                    )),
                reasoning_tokens INTEGER
                    CHECK(reasoning_tokens IS NULL OR (
                        typeof(reasoning_tokens) = 'integer' AND reasoning_tokens >= 0
                    )),
                total_tokens INTEGER
                    CHECK(total_tokens IS NULL OR (
                        typeof(total_tokens) = 'integer' AND total_tokens >= 0
                    )),
                status TEXT NOT NULL
                    CHECK(status IN ('started', 'completed', 'failed', 'cancelled')),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                event_time TEXT NOT NULL,
                error_type TEXT
                    CHECK(error_type IS NULL OR (
                        typeof(error_type) = 'text' AND length(error_type) BETWEEN 1 AND 128
                    )),
                created_at TEXT NOT NULL,
                UNIQUE(attempt_id, status)
            )
            """,
            """
            CREATE INDEX remote_call_ledger_job_idx
            ON remote_call_ledger(job_id, id)
            """,
            """
            CREATE INDEX remote_call_ledger_attempt_idx
            ON remote_call_ledger(attempt_id, id)
            """,
            """
            CREATE TRIGGER remote_call_ledger_append_only_update_guard
            BEFORE UPDATE ON remote_call_ledger
            BEGIN
                SELECT RAISE(ABORT, 'remote_call_ledger is append-only');
            END
            """,
            """
            CREATE TRIGGER remote_call_ledger_append_only_delete_guard
            BEFORE DELETE ON remote_call_ledger
            BEGIN
                SELECT RAISE(ABORT, 'remote_call_ledger is append-only');
            END
            """,
        ),
    ),
    (
        9,
        (
            """
            ALTER TABLE job_events ADD COLUMN payload_json TEXT
                CHECK(payload_json IS NULL OR (
                    typeof(payload_json) = 'text' AND length(payload_json) <= 4096
                ))
            """,
            """
            ALTER TABLE job_commands ADD COLUMN request_status TEXT NOT NULL
                DEFAULT 'accepted'
                CHECK(request_status IN ('accepted', 'rejected'))
            """,
            """
            ALTER TABLE job_commands ADD COLUMN rejection_code TEXT
                CHECK(rejection_code IS NULL OR (
                    typeof(rejection_code) = 'text'
                    AND length(rejection_code) BETWEEN 1 AND 128
                ))
            """,
            """
            ALTER TABLE job_commands ADD COLUMN result_code TEXT
                CHECK(result_code IS NULL OR (
                    typeof(result_code) = 'text'
                    AND length(result_code) BETWEEN 1 AND 128
                ))
            """,
            "ALTER TABLE job_commands ADD COLUMN updated_at TEXT",
            """
            UPDATE job_commands
            SET updated_at = COALESCE(completed_at, claimed_at, created_at)
            WHERE updated_at IS NULL
            """,
            """
            CREATE INDEX job_commands_job_idx
            ON job_commands(job_id, id)
            """,
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


def _applied_migration_versions(connection: sqlite3.Connection) -> set[int]:
    return {
        row["version"]
        for row in connection.execute("SELECT version FROM schema_migrations")
    }


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
            for version, statements in MIGRATIONS:
                rebuilds_jobs_table = version == 7
                if rebuilds_jobs_table:
                    connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _ensure_schema_migrations_table(connection)
                    applied = _applied_migration_versions(connection)
                    if version in applied:
                        connection.commit()
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
                    if rebuilds_jobs_table:
                        connection.execute("PRAGMA foreign_keys = ON")
        finally:
            connection.close()
    return database_path
