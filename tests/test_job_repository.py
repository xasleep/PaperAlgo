import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

from web_api import config, database as database_module
from web_api.database import connect_database, initialize_database
from web_api.errors import (
    IdempotencyConflictError,
    InvalidStateTransitionError,
    OptimisticLockConflictError,
)
from web_api.job_repository import JobRepository


def _request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "upload_id": "0" * 32,
        "paper_name": "paper",
        "domain": "statistics",
        "eval_type": "ref_free",
        "generated_n": 1,
        "auto_refine": False,
        "max_repair_rounds": 0,
        "console_output": "quiet",
        "skip_mineru": False,
        "pdf_markdown_path": "",
    }
    request.update(overrides)
    return request


def test_migrations_are_repeatable_and_enable_required_pragmas(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "paper2code.db"

    initialize_database(db_path)
    initialize_database(db_path)

    with closing(connect_database(db_path)) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert {
        "jobs",
        "stage_runs",
        "job_events",
        "job_commands",
        "cost_entries",
        "worker_leases",
        "job_processes",
    }.issubset(tables)
    assert versions == [1, 2]
    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 5000


def test_concurrent_first_initialization_is_serialized(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent" / "paper2code.db"
    worker_count = 16
    barrier = Barrier(worker_count)

    def initialize_from_worker() -> Path:
        barrier.wait()
        return initialize_database(db_path)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        initialized_paths = list(
            executor.map(lambda _: initialize_from_worker(), range(worker_count))
        )

    assert initialized_paths == [
        database_module.normalize_database_path(db_path)
    ] * worker_count
    with closing(connect_database(db_path)) as connection:
        versions = connection.execute(
            "SELECT version, COUNT(*) AS count "
            "FROM schema_migrations GROUP BY version ORDER BY version"
        ).fetchall()
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert [(row["version"], row["count"]) for row in versions] == [
        (1, 1),
        (2, 1),
    ]
    assert {
        "jobs",
        "stage_runs",
        "job_events",
        "job_commands",
        "cost_entries",
        "worker_leases",
        "job_processes",
    }.issubset(tables)


def test_failed_migration_rolls_back_schema_and_can_be_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "failed" / "paper2code.db"
    valid_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (
            (
                1,
                (
                    "CREATE TABLE partial_table (id INTEGER PRIMARY KEY)",
                    "CREATE TABLE broken syntax",
                ),
            ),
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        initialize_database(db_path)

    with closing(sqlite3.connect(db_path)) as connection:
        objects = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
            if not row[0].startswith("sqlite_")
        }
    assert "schema_migrations" not in objects
    assert "partial_table" not in objects

    monkeypatch.setattr(database_module, "MIGRATIONS", valid_migrations)
    initialize_database(db_path)
    with closing(connect_database(db_path)) as connection:
        assert [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        ] == [1, 2]
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0] == 0


def test_connection_configuration_failure_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "failed-connect" / "paper2code.db"

    class FailingConnection:
        row_factory = None
        closed = False

        def execute(self, statement: str):
            raise sqlite3.OperationalError(f"configuration failed: {statement[:6]}")

        def close(self) -> None:
            self.closed = True

    connection = FailingConnection()
    monkeypatch.setattr(
        database_module.sqlite3,
        "connect",
        lambda *args, **kwargs: connection,
    )

    with pytest.raises(sqlite3.OperationalError):
        connect_database(db_path)

    assert connection.closed is True
    assert not db_path.exists()


def test_wal_configuration_failure_closes_and_removes_new_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "failed-wal" / "paper2code.db"

    def fail_wal(connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("WAL configuration failed")

    monkeypatch.setattr(database_module, "_enable_wal", fail_wal)

    with pytest.raises(sqlite3.OperationalError, match="WAL configuration failed"):
        initialize_database(db_path)

    assert not list(db_path.parent.glob("paper2code.db*"))
    assert not list(db_path.parent.glob("*.tmp"))


def test_state_transitions_use_one_validated_entry(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    job, created = repository.create_job(
        job_id="job_state",
        request=_request(),
        paper_name="paper",
    )

    assert created is True
    assert job["execution_status"] == "queued"
    assert job["evaluation_status"] == "pending"
    assert job["quality_status"] == "pending"
    assert job["version"] == 1

    running = repository.transition_job(
        "job_state",
        expected_version=1,
        execution_status="running",
    )
    evaluating = repository.transition_job(
        "job_state",
        expected_version=2,
        execution_status="completed",
        evaluation_status="running",
    )
    assessing = repository.transition_job(
        "job_state",
        expected_version=3,
        evaluation_status="passed",
        quality_status="assessing",
    )

    assert running["version"] == 2
    assert evaluating["version"] == 3
    assert evaluating["execution_status"] == "completed"
    assert assessing["version"] == 4
    assert assessing["evaluation_status"] == "passed"
    assert assessing["quality_status"] == "assessing"
    with pytest.raises(InvalidStateTransitionError) as no_change:
        repository.transition_job(
            "job_state",
            expected_version=4,
            evaluation_status="passed",
        )
    assert no_change.value.details == {"reason": "no_status_change"}
    with pytest.raises(InvalidStateTransitionError):
        repository.transition_job(
            "job_state",
            expected_version=4,
            execution_status="queued",
        )


@pytest.mark.parametrize(
    ("setup_execution", "transition"),
    [
        (None, {"evaluation_status": "passed"}),
        (None, {"quality_status": "accepted"}),
        ("running", {"evaluation_status": "running"}),
        ("failed", {"evaluation_status": "passed"}),
        ("canceled", {"evaluation_status": "passed"}),
        (
            "running",
            {
                "execution_status": "completed",
                "evaluation_status": "running",
                "quality_status": "assessing",
            },
        ),
    ],
)
def test_invalid_composite_states_are_rejected_without_database_changes(
    tmp_path: Path,
    setup_execution: str | None,
    transition: dict[str, str],
) -> None:
    db_path = tmp_path / "paper2code.db"
    repository = JobRepository(db_path)
    repository.create_job(
        job_id="job_composite",
        request=_request(),
        paper_name="paper",
    )
    expected_version = 1
    if setup_execution is not None:
        repository.transition_job(
            "job_composite",
            expected_version=expected_version,
            execution_status=setup_execution,
        )
        expected_version += 1

    before = repository.get_job("job_composite")
    with closing(connect_database(db_path)) as connection:
        events_before = connection.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id = 'job_composite'"
        ).fetchone()[0]

    with pytest.raises(InvalidStateTransitionError) as exc_info:
        repository.transition_job(
            "job_composite",
            expected_version=expected_version,
            **transition,
        )

    assert exc_info.value.details == {
        "reason": "invalid_composite_state",
        "execution_status": transition.get(
            "execution_status", before["execution_status"]
        ),
        "evaluation_status": transition.get(
            "evaluation_status", before["evaluation_status"]
        ),
        "quality_status": transition.get("quality_status", before["quality_status"]),
    }
    assert repository.get_job("job_composite") == before
    with closing(connect_database(db_path)) as connection:
        events_after = connection.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id = 'job_composite'"
        ).fetchone()[0]
    assert events_after == events_before


def test_valid_composite_terminal_transitions_are_allowed(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(
        job_id="job_completed",
        request=_request(),
        paper_name="paper",
    )
    repository.transition_job(
        "job_completed",
        expected_version=1,
        execution_status="running",
    )
    completed = repository.transition_job(
        "job_completed",
        expected_version=2,
        execution_status="completed",
        evaluation_status="passed",
        quality_status="assessing",
    )

    repository.create_job(
        job_id="job_failed",
        request=_request(),
        paper_name="paper",
    )
    failed = repository.transition_job(
        "job_failed",
        expected_version=1,
        execution_status="failed",
        evaluation_status="skipped",
        quality_status="skipped",
    )

    assert (
        completed["execution_status"],
        completed["evaluation_status"],
        completed["quality_status"],
    ) == ("completed", "passed", "assessing")
    assert (
        failed["execution_status"],
        failed["evaluation_status"],
        failed["quality_status"],
    ) == ("failed", "skipped", "skipped")


def test_optimistic_lock_conflict_is_detected(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(
        job_id="job_version",
        request=_request(),
        paper_name="paper",
    )
    repository.transition_job(
        "job_version",
        expected_version=1,
        execution_status="running",
    )

    with pytest.raises(OptimisticLockConflictError):
        repository.transition_job(
            "job_version",
            expected_version=1,
            evaluation_status="running",
        )


def test_idempotent_create_reuses_job_and_rejects_payload_mismatch(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")

    first, first_created = repository.create_job(
        job_id="job_first",
        request=_request(),
        paper_name="paper",
        idempotency_key="same-request-key",
    )
    replay, replay_created = repository.create_job(
        job_id="job_second",
        request=_request(),
        paper_name="paper",
        idempotency_key="same-request-key",
    )

    assert first_created is True
    assert replay_created is False
    assert replay["job_id"] == first["job_id"] == "job_first"
    with pytest.raises(IdempotencyConflictError):
        repository.create_job(
            job_id="job_third",
            request=_request(generated_n=2),
            paper_name="paper",
            idempotency_key="same-request-key",
        )
    assert len(repository.list_jobs()) == 1


def test_cost_entries_require_integer_microusd(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(
        job_id="job_cost",
        request=_request(),
        paper_name="paper",
    )

    entry_id = repository.add_cost_entry(
        "job_cost",
        provider="fake",
        model="fake-model",
        category="planning",
        amount_microusd=1234,
        input_tokens=10,
        output_tokens=20,
    )

    with pytest.raises(ValueError):
        repository.add_cost_entry(
            "job_cost",
            provider="fake",
            model="fake-model",
            category="planning",
            amount_microusd=1.5,  # type: ignore[arg-type]
        )
    with closing(connect_database(tmp_path / "paper2code.db")) as connection:
        row = connection.execute(
            "SELECT amount_microusd, typeof(amount_microusd) AS storage_type "
            "FROM cost_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO cost_entries "
                "(job_id, provider, model, category, amount_microusd, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("job_cost", "fake", "fake-model", "planning", 2.5, "now"),
            )

    assert row["amount_microusd"] == 1234
    assert row["storage_type"] == "integer"


def test_schema_has_no_secret_or_full_model_payload_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "paper2code.db"
    initialize_database(db_path)

    with closing(connect_database(db_path)) as connection:
        schema = "\n".join(
            row["sql"] or ""
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table'"
            )
        ).lower()

    assert "api_key" not in schema
    assert "prompt" not in schema
    assert "response" not in schema
    assert "model_response" not in schema
    assert "amount_microusd" in schema


def test_repository_rejects_unvalidated_sensitive_request_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "paper2code.db"
    repository = JobRepository(db_path)
    secret = "UNIQUE-" + "REPOSITORY-SECRET"

    with pytest.raises(ValueError):
        repository.create_job(
            job_id="job_secret",
            request={**_request(), "api_key": secret},
            paper_name="paper",
        )

    with closing(connect_database(db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    for database_file in db_path.parent.glob("paper2code.db*"):
        if secret.encode() in database_file.read_bytes():
            pytest.fail("SQLite files contain rejected sensitive input", pytrace=False)


def test_database_path_defaults_to_local_and_supports_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    default_path = tmp_path / ".local" / "paper2code.db"
    override_path = tmp_path / "override" / "state.db"
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", default_path)
    monkeypatch.delenv("PAPER2CODE_DB_PATH", raising=False)

    assert config.configured_database_path() == default_path

    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(override_path))
    assert config.configured_database_path() == override_path
