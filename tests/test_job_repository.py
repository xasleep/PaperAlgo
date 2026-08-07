import sqlite3
import time
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
    JobNotCancelableError,
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


def _provider_snapshot(**overrides: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "reproduce_provider": "openai",
        "reproduce_model": "gpt-4.1-mini",
        "evaluation_provider": "openai",
        "evaluation_model": "gpt-4.1-mini",
        "evaluation_fallback_models": ["gpt-4o-mini"],
        "provider_registry_version": 1,
        "provider_contract_fingerprint": "a" * 64,
    }
    snapshot.update(overrides)
    return snapshot


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
        "job_process_history",
    }.issubset(tables)
    assert versions == [1, 2, 3, 4, 5, 6]
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
        (3, 1),
        (4, 1),
        (5, 1),
        (6, 1),
    ]
    assert {
        "jobs",
        "stage_runs",
        "job_events",
        "job_commands",
        "cost_entries",
        "worker_leases",
        "job_processes",
        "job_process_history",
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
        ] == [1, 2, 3, 4, 5, 6]
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0] == 0


def test_version_2_database_upgrades_repeatably_without_trusting_legacy_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "version-2" / "paper2code.db"
    current_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations[:2])
    initialize_database(db_path)
    with closing(connect_database(db_path)) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO worker_leases (
                    lease_name, owner_id, expires_at, version, acquired_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "pipeline-worker",
                    "legacy-worker",
                    "2999-01-01T00:00:00.000Z",
                    7,
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                ),
            )

    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations)
    initialize_database(db_path)
    initialize_database(db_path)

    with closing(connect_database(db_path)) as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(worker_leases)")
        }
        legacy_row = connection.execute(
            "SELECT owner_token FROM worker_leases WHERE lease_name = ?",
            ("pipeline-worker",),
        ).fetchone()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert versions == [1, 2, 3, 4, 5, 6]
    assert "owner_token" in columns
    assert legacy_row["owner_token"] is None
    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 5000

    repository = JobRepository(db_path)
    assert (
        repository.acquire_worker_lease("legacy-worker", "fresh-token") is False
    )
    with closing(connect_database(db_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE worker_leases SET expires_at = ? WHERE lease_name = ?",
                ("2000-01-01T00:00:00.000Z", "pipeline-worker"),
            )
    assert repository.acquire_worker_lease("legacy-worker", "fresh-token") is True


def test_version_3_database_upgrades_launch_states_without_losing_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "version-3" / "paper2code.db"
    current_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations[:3])
    initialize_database(db_path)

    def insert_job(connection: sqlite3.Connection, job_id: str) -> None:
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, request_hash, paper_name, upload_id, domain, eval_type,
                generated_n, auto_refine, max_repair_rounds, console_output,
                skip_mineru, pdf_markdown_path, execution_status,
                evaluation_status, quality_status, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                "0" * 64,
                "paper",
                "0" * 32,
                "statistics",
                "ref_free",
                1,
                0,
                0,
                "quiet",
                0,
                "",
                "running",
                "pending",
                "pending",
                2,
                "2026-01-01T00:00:00.000Z",
                "2026-01-01T00:00:00.000Z",
            ),
        )

    with closing(connect_database(db_path)) as connection:
        with connection:
            for job_id in ("claimed_job", "registered_job", "exited_job"):
                insert_job(connection, job_id)
            connection.executemany(
                """
                INSERT INTO job_processes (
                    job_id, worker_id, launch_token, pid, process_create_time,
                    process_group_id, heartbeat_at, started_at, exited_at, exit_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        "claimed_job",
                        "worker",
                        "claimed-launch",
                        None,
                        None,
                        None,
                        "2026-01-01T00:00:00.000Z",
                        None,
                        None,
                        None,
                    ),
                    (
                        "registered_job",
                        "worker",
                        "registered-launch",
                        1001,
                        "create-1001",
                        1001,
                        "2026-01-01T00:00:00.000Z",
                        "2026-01-01T00:00:00.000Z",
                        None,
                        None,
                    ),
                    (
                        "exited_job",
                        "worker",
                        "exited-launch",
                        1002,
                        "create-1002",
                        1002,
                        "2026-01-01T00:00:00.000Z",
                        "2026-01-01T00:00:00.000Z",
                        "2026-01-01T00:01:00.000Z",
                        0,
                    ),
                ),
            )

    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations)
    initialize_database(db_path)
    initialize_database(db_path)

    with closing(connect_database(db_path)) as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        states = {
            row["job_id"]: (row["launch_state"], row["launch_error_code"])
            for row in connection.execute(
                "SELECT job_id, launch_state, launch_error_code FROM job_processes"
            )
        }
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE job_processes SET launch_state = 'unsafe' "
                "WHERE job_id = 'claimed_job'"
            )

    assert versions == [1, 2, 3, 4, 5, 6]
    assert states == {
        "claimed_job": ("claimed", None),
        "registered_job": ("registered", None),
        "exited_job": ("exited", None),
    }


def test_failed_migration_4_rolls_back_added_launch_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "failed-version-4" / "paper2code.db"
    current_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations[:3])
    initialize_database(db_path)
    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        current_migrations[:3]
        + (
            (
                4,
                (
                    "ALTER TABLE job_processes ADD COLUMN launch_state TEXT",
                    "CREATE TABLE broken syntax",
                ),
            ),
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        initialize_database(db_path)

    with closing(connect_database(db_path)) as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(job_processes)")
        }
    assert versions == [1, 2, 3]
    assert "launch_state" not in columns

    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations)
    initialize_database(db_path)
    with closing(connect_database(db_path)) as connection:
        assert [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2, 3, 4, 5, 6]


def test_version_4_database_upgrades_recovery_schema_without_losing_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "version-4" / "paper2code.db"
    current_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations[:4])
    initialize_database(db_path)
    with closing(connect_database(db_path)) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, request_hash, paper_name, upload_id, domain, eval_type,
                    generated_n, auto_refine, max_repair_rounds, console_output,
                    skip_mineru, pdf_markdown_path, execution_status,
                    evaluation_status, quality_status, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "upgrade_job",
                    "0" * 64,
                    "paper",
                    "0" * 32,
                    "statistics",
                    "ref_free",
                    1,
                    0,
                    0,
                    "quiet",
                    0,
                    "",
                    "running",
                    "pending",
                    "pending",
                    2,
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO job_processes (
                    job_id, worker_id, launch_token, pid, process_create_time,
                    process_group_id, heartbeat_at, started_at, launch_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'registered')
                """,
                (
                    "upgrade_job",
                    "worker",
                    "upgrade-launch",
                    1234,
                    "create-1234",
                    1234,
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO stage_runs (
                    job_id, stage_name, attempt, status, version,
                    created_at, updated_at
                ) VALUES (?, 'legacy_stage', 1, 'legacy_status', 1, ?, ?)
                """,
                (
                    "upgrade_job",
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                ),
            )

    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations)
    initialize_database(db_path)
    initialize_database(db_path)

    with closing(connect_database(db_path)) as connection:
        job = connection.execute(
            "SELECT * FROM jobs WHERE job_id = 'upgrade_job'"
        ).fetchone()
        process = connection.execute(
            "SELECT * FROM job_processes WHERE job_id = 'upgrade_job'"
        ).fetchone()
        stage = connection.execute(
            "SELECT * FROM stage_runs WHERE job_id = 'upgrade_job'"
        ).fetchone()
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO stage_runs (
                    job_id, stage_name, attempt, status, created_at, updated_at
                ) VALUES ('upgrade_job', 'unsafe', 2, 'completed', 'now', 'now')
                """
            )

    assert versions == [1, 2, 3, 4, 5, 6]
    assert job["recovery_count"] == 0
    assert job["recovery_status"] == "none"
    assert process["process_attempt"] == 1
    assert stage["stage_name"] == "legacy_stage"
    assert stage["status"] == "legacy_status"
    assert stage["stage_sequence"] is None
    assert stage["resume_eligible"] == 0


def test_failed_migration_5_rolls_back_recovery_columns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "failed-version-5" / "paper2code.db"
    current_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations[:4])
    initialize_database(db_path)
    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        current_migrations[:4]
        + (
            (
                5,
                (
                    "ALTER TABLE jobs ADD COLUMN recovery_count INTEGER",
                    "CREATE TABLE broken syntax",
                ),
            ),
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        initialize_database(db_path)

    with closing(connect_database(db_path)) as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
        }
    assert versions == [1, 2, 3, 4]
    assert "recovery_count" not in columns

    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations)
    initialize_database(db_path)
    with closing(connect_database(db_path)) as connection:
        assert [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2, 3, 4, 5, 6]


def test_different_worker_ids_cannot_share_active_global_lease(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")

    assert repository.acquire_worker_lease("worker-a", "token-a") is True
    assert repository.acquire_worker_lease("worker-b", "token-b") is False


def test_same_worker_id_with_different_tokens_is_fenced(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(job_id="same_name_job", request=_request(), paper_name="paper")

    assert repository.acquire_worker_lease("shared-name", "token-a") is True
    assert repository.acquire_worker_lease("shared-name", "token-b") is False
    with pytest.raises(RuntimeError, match="lease"):
        repository.claim_next_queued_job(
            worker_id="shared-name",
            instance_token="token-b",
            launch_token="loser-launch",
        )
    assert repository.get_job("same_name_job")["execution_status"] == "queued"


def test_concurrent_same_name_lease_acquire_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    JobRepository(db_path)
    barrier = Barrier(2)

    def acquire(instance_token: str) -> bool:
        repository = JobRepository(db_path)
        barrier.wait()
        return repository.acquire_worker_lease("shared-name", instance_token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, ("token-a", "token-b")))

    assert sorted(results) == [False, True]


def test_concurrent_same_name_workers_can_claim_at_most_one_job(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    repository = JobRepository(db_path)
    repository.create_job(job_id="job_a", request=_request(), paper_name="paper")
    repository.create_job(job_id="job_b", request=_request(), paper_name="paper")
    barrier = Barrier(2)

    def acquire_and_claim(instance_token: str) -> str | None:
        contender = JobRepository(db_path)
        barrier.wait()
        if not contender.acquire_worker_lease("shared-name", instance_token):
            return None
        claimed = contender.claim_next_queued_job(
            worker_id="shared-name",
            instance_token=instance_token,
            launch_token=f"launch-{instance_token}",
        )
        return None if claimed is None else str(claimed["job_id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed_job_ids = list(
            executor.map(acquire_and_claim, ("token-a", "token-b"))
        )

    assert sum(job_id is not None for job_id in claimed_job_ids) == 1
    statuses = [job["execution_status"] for job in repository.list_jobs(limit=10)]
    assert statuses.count("running") == 1
    assert statuses.count("queued") == 1


def test_same_worker_instance_renews_lease_without_resetting_acquired_at(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    repository = JobRepository(db_path)
    assert repository.acquire_worker_lease("worker", "stable-token") is True
    with closing(connect_database(db_path)) as connection:
        first = dict(
            connection.execute(
                "SELECT * FROM worker_leases WHERE lease_name = ?",
                ("pipeline-worker",),
            ).fetchone()
        )

    time.sleep(0.02)
    assert repository.acquire_worker_lease("worker", "stable-token") is True
    with closing(connect_database(db_path)) as connection:
        renewed = dict(
            connection.execute(
                "SELECT * FROM worker_leases WHERE lease_name = ?",
                ("pipeline-worker",),
            ).fetchone()
        )

    assert renewed["version"] == first["version"] + 1
    assert renewed["updated_at"] > first["updated_at"]
    assert renewed["acquired_at"] == first["acquired_at"]


def test_expired_takeover_fences_every_old_worker_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "paper2code.db"
    repository = JobRepository(db_path)
    repository.create_job(job_id="running_job", request=_request(), paper_name="paper")
    repository.create_job(job_id="queued_job", request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("shared-name", "old-token") is True
    claimed = repository.claim_next_queued_job(
        worker_id="shared-name",
        instance_token="old-token",
        launch_token="old-launch",
    )
    assert claimed is not None
    repository.request_cancel("running_job")
    repository.request_cancel("queued_job")
    with closing(connect_database(db_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE worker_leases SET expires_at = ? WHERE lease_name = ?",
                ("2000-01-01T00:00:00.000Z", "pipeline-worker"),
            )

    assert repository.acquire_worker_lease("shared-name", "new-token") is True

    fenced_calls = (
        lambda: repository.claim_next_queued_job(
            worker_id="shared-name",
            instance_token="old-token",
            launch_token="stale-claim",
        ),
        lambda: repository.cancel_next_queued_job("shared-name", "old-token"),
        lambda: repository.claim_cancel_command(
            "running_job", worker_id="shared-name", instance_token="old-token"
        ),
        lambda: repository.record_process_started(
            "running_job",
            worker_id="shared-name",
            instance_token="old-token",
            launch_token="old-launch",
            pid=1234,
            process_create_time="2026-01-01T00:00:00.000Z",
            process_group_id=1234,
            command_summary="python fake_pipeline.py --job-id running_job",
        ),
        lambda: repository.heartbeat_process(
            "running_job",
            worker_id="shared-name",
            instance_token="old-token",
            launch_token="old-launch",
        ),
        lambda: repository.record_completed_checkpoint(
            "running_job",
            worker_id="shared-name",
            instance_token="old-token",
            launch_token="old-launch",
            checkpoint={
                "version": 1,
                "stage_name": "planning",
                "stage_sequence": 2,
                "stage_attempt": 1,
                "status": "completed",
                "resume_from_stage": "extract_config",
            },
            checkpoint_path="checkpoints/checkpoint-0002-planning-001.json",
        ),
        lambda: repository.prepare_recovery_attempt(
            "running_job",
            worker_id="shared-name",
            instance_token="old-token",
            launch_token="old-launch",
            new_launch_token="stale-recovery-launch",
            resume_from_stage="extract_config",
            resume_stage_sequence=3,
        ),
        lambda: repository.finish_process(
            "running_job",
            worker_id="shared-name",
            instance_token="old-token",
            launch_token="old-launch",
            exit_code=0,
        ),
        lambda: repository.complete_cancellation(
            "running_job",
            worker_id="shared-name",
            instance_token="old-token",
            launch_token="old-launch",
            exit_code=-1,
        ),
        lambda: repository.fail_process(
            "running_job",
            worker_id="shared-name",
            instance_token="old-token",
            launch_token="old-launch",
            failure_code="stale-worker",
        ),
    )
    for fenced_call in fenced_calls:
        with pytest.raises(RuntimeError, match="lease"):
            fenced_call()

    assert repository.release_worker_lease("shared-name", "old-token") is False
    with closing(connect_database(db_path)) as connection:
        current = connection.execute(
            "SELECT owner_id, owner_token FROM worker_leases WHERE lease_name = ?",
            ("pipeline-worker",),
        ).fetchone()
    assert (current["owner_id"], current["owner_token"]) == (
        "shared-name",
        "new-token",
    )


def test_current_lease_cannot_claim_second_job_while_one_is_running(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(job_id="job_first", request=_request(), paper_name="paper")
    repository.create_job(job_id="job_second", request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("worker", "token") is True

    first = repository.claim_next_queued_job(
        worker_id="worker",
        instance_token="token",
        launch_token="launch-first",
    )
    second = repository.claim_next_queued_job(
        worker_id="worker",
        instance_token="token",
        launch_token="launch-second",
    )

    assert first is not None
    assert second is None
    assert repository.get_job("job_first")["execution_status"] == "running"
    assert repository.get_job("job_second")["execution_status"] == "queued"


def test_stage_checkpoint_transitions_are_atomic_idempotent_and_fenced(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(job_id="stage_job", request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("worker", "token")
    repository.claim_next_queued_job(
        worker_id="worker",
        instance_token="token",
        launch_token="stage-launch",
    )
    repository.record_process_started(
        "stage_job",
        worker_id="worker",
        instance_token="token",
        launch_token="stage-launch",
        pid=1234,
        process_create_time="create-1234",
        process_group_id=1234,
        command_summary="python run_pipeline.py --job-id redacted",
    )
    running = {
        "version": 1,
        "stage_name": "planning",
        "stage_sequence": 2,
        "stage_attempt": 2,
        "status": "running",
        "resume_from_stage": None,
        "error_code": None,
    }
    checkpoint_path = "checkpoints/checkpoint-0002-planning-002.json"

    started = repository.record_stage_checkpoint(
        "stage_job",
        worker_id="worker",
        instance_token="token",
        launch_token="stage-launch",
        checkpoint=running,
        checkpoint_path=checkpoint_path,
    )
    version_after_start = repository.get_job("stage_job")["version"]
    repeated = repository.record_stage_checkpoint(
        "stage_job",
        worker_id="worker",
        instance_token="token",
        launch_token="stage-launch",
        checkpoint=running,
        checkpoint_path=checkpoint_path,
    )
    assert repeated["id"] == started["id"]
    assert repository.get_job("stage_job")["version"] == version_after_start

    completed = repository.record_completed_checkpoint(
        "stage_job",
        worker_id="worker",
        instance_token="token",
        launch_token="stage-launch",
        checkpoint={
            **running,
            "status": "completed",
            "resume_from_stage": "extract_config",
        },
        checkpoint_path=checkpoint_path,
    )

    assert completed["status"] == "completed"
    assert completed["resume_eligible"] == 1
    assert completed["version"] == 2
    job = repository.get_job("stage_job")
    assert job["current_stage"] == "planning"
    assert job["current_stage_attempt"] == 2
    assert job["last_checkpoint_stage"] == "planning"
    assert job["version"] == version_after_start + 1
    with closing(connect_database(repository.database_path)) as connection:
        event_types = [
            row["event_type"]
            for row in connection.execute(
                "SELECT event_type FROM job_events WHERE job_id = ? ORDER BY id",
                ("stage_job",),
            )
        ]
    assert event_types.count("job.stage_running") == 1
    assert event_types.count("job.stage_completed") == 1


def test_claim_registration_and_unresolved_launch_state_transitions(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(job_id="launch_job", request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("worker", "token") is True
    repository.claim_next_queued_job(
        worker_id="worker",
        instance_token="token",
        launch_token="launch-token",
    )

    claimed = repository.get_process("launch_job")
    assert claimed["launch_state"] == "claimed"
    assert claimed["launch_error_code"] is None

    unresolved = repository.mark_launch_identity_unresolved(
        "launch_job",
        worker_id="worker",
        instance_token="token",
        launch_token="launch-token",
    )
    assert unresolved["launch_state"] == "identity_unresolved"
    assert unresolved["launch_error_code"] == "process_identity_unresolved"

    registered = repository.record_process_started(
        "launch_job",
        worker_id="worker",
        instance_token="token",
        launch_token="launch-token",
        pid=1234,
        process_create_time="create-1234",
        process_group_id=1234,
        command_summary="python fake_pipeline.py --job-id launch_job",
    )
    assert registered["launch_state"] == "registered"
    assert registered["launch_error_code"] is None
    with pytest.raises(OptimisticLockConflictError):
        repository.record_process_started(
            "launch_job",
            worker_id="worker",
            instance_token="token",
            launch_token="launch-token",
            pid=5678,
            process_create_time="create-5678",
            process_group_id=5678,
            command_summary="python other.py",
        )
    assert repository.get_process("launch_job")["pid"] == 1234


def test_mark_launch_identity_unresolved_is_idempotent_and_blocks_cancel(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    repository = JobRepository(db_path)
    repository.create_job(job_id="unknown_launch", request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("worker", "token") is True
    repository.claim_next_queued_job(
        worker_id="worker",
        instance_token="token",
        launch_token="unknown-token",
    )
    pending = repository.request_cancel("unknown_launch")
    version_before = repository.get_job("unknown_launch")["version"]

    first = repository.mark_launch_identity_unresolved(
        "unknown_launch",
        worker_id="worker",
        instance_token="token",
        launch_token="unknown-token",
    )
    repeated = repository.mark_launch_identity_unresolved(
        "unknown_launch",
        worker_id="worker",
        instance_token="token",
        launch_token="unknown-token",
    )

    assert first == repeated
    assert first["launch_state"] == "identity_unresolved"
    assert repository.get_job("unknown_launch")["execution_status"] == "running"
    assert repository.get_job("unknown_launch")["version"] == version_before
    command = repository.get_cancel_command("unknown_launch")
    assert command["id"] == pending["id"]
    assert command["status"] == "failed"
    assert command["error_code"] == "process_identity_unresolved"
    with pytest.raises(JobNotCancelableError) as exc_info:
        repository.request_cancel("unknown_launch")
    assert exc_info.value.reason == "process_identity_unresolved"
    with closing(connect_database(db_path)) as connection:
        unresolved_events = connection.execute(
            """
            SELECT COUNT(*) FROM job_events
            WHERE job_id = ? AND event_type = 'job.launch_identity_unresolved'
            """,
            ("unknown_launch",),
        ).fetchone()[0]
    assert unresolved_events == 1


def test_lost_lease_cannot_mark_launch_identity_unresolved(tmp_path: Path) -> None:
    db_path = tmp_path / "paper2code.db"
    repository = JobRepository(db_path)
    repository.create_job(job_id="fenced_launch", request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("shared", "old-token") is True
    repository.claim_next_queued_job(
        worker_id="shared",
        instance_token="old-token",
        launch_token="fenced-token",
    )
    with closing(connect_database(db_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE worker_leases SET expires_at = ? WHERE lease_name = ?",
                ("2000-01-01T00:00:00.000Z", "pipeline-worker"),
            )
    assert repository.acquire_worker_lease("shared", "new-token") is True

    with pytest.raises(RuntimeError, match="lease"):
        repository.mark_launch_identity_unresolved(
            "fenced_launch",
            worker_id="shared",
            instance_token="old-token",
            launch_token="fenced-token",
        )
    assert repository.get_process("fenced_launch")["launch_state"] == "claimed"


@pytest.mark.parametrize("terminal_operation", ["finish", "cancel", "fail"])
def test_terminal_process_updates_launch_state_to_exited(
    tmp_path: Path,
    terminal_operation: str,
) -> None:
    repository = JobRepository(tmp_path / terminal_operation / "paper2code.db")
    job_id = f"{terminal_operation}_job"
    repository.create_job(job_id=job_id, request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("worker", "token") is True
    repository.claim_next_queued_job(
        worker_id="worker",
        instance_token="token",
        launch_token="launch-token",
    )
    repository.record_process_started(
        job_id,
        worker_id="worker",
        instance_token="token",
        launch_token="launch-token",
        pid=1234,
        process_create_time="create-1234",
        process_group_id=1234,
        command_summary=f"python fake.py --job-id {job_id}",
    )

    if terminal_operation == "finish":
        repository.finish_process(
            job_id,
            worker_id="worker",
            instance_token="token",
            launch_token="launch-token",
            exit_code=0,
        )
    elif terminal_operation == "cancel":
        repository.request_cancel(job_id)
        repository.claim_cancel_command(
            job_id,
            worker_id="worker",
            instance_token="token",
        )
        repository.complete_cancellation(
            job_id,
            worker_id="worker",
            instance_token="token",
            launch_token="launch-token",
            exit_code=-1,
        )
    else:
        repository.fail_process(
            job_id,
            worker_id="worker",
            instance_token="token",
            launch_token="launch-token",
            failure_code="test_failure",
        )

    assert repository.get_process(job_id)["launch_state"] == "exited"


@pytest.mark.parametrize("cancel_command_state", [None, "failed", "completed"])
def test_complete_cancellation_requires_an_active_cancel_command(
    tmp_path: Path,
    cancel_command_state: str | None,
) -> None:
    db_path = tmp_path / str(cancel_command_state) / "paper2code.db"
    repository = JobRepository(db_path)
    repository.create_job(job_id="cancel_guard", request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("worker", "token") is True
    repository.claim_next_queued_job(
        worker_id="worker",
        instance_token="token",
        launch_token="launch-token",
    )
    repository.record_process_started(
        "cancel_guard",
        worker_id="worker",
        instance_token="token",
        launch_token="launch-token",
        pid=1234,
        process_create_time="create-1234",
        process_group_id=1234,
        command_summary="fake-pipeline --job-id cancel_guard",
    )
    if cancel_command_state is not None:
        repository.request_cancel("cancel_guard")
        with closing(connect_database(db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE job_commands
                    SET status = ?, completed_at = '2026-01-01T00:00:00.000Z'
                    WHERE job_id = ? AND command_type = 'cancel'
                    """,
                    (cancel_command_state, "cancel_guard"),
                )

    with pytest.raises(OptimisticLockConflictError):
        repository.complete_cancellation(
            "cancel_guard",
            worker_id="worker",
            instance_token="token",
            launch_token="launch-token",
            exit_code=-1,
        )

    assert repository.get_job("cancel_guard")["execution_status"] == "running"
    assert repository.get_process("cancel_guard")["launch_state"] == "registered"


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


def test_migration_6_adds_non_sensitive_provider_snapshot_repeatably_and_keeps_old_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "version-5" / "paper2code.db"
    current_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations[:5])
    initialize_database(db_path)
    with closing(connect_database(db_path)) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, request_hash, paper_name, upload_id, domain, eval_type,
                    generated_n, auto_refine, max_repair_rounds, console_output,
                    skip_mineru, pdf_markdown_path, execution_status,
                    evaluation_status, quality_status, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "historical_completed",
                    "0" * 64,
                    "paper",
                    "0" * 32,
                    "statistics",
                    "ref_free",
                    1,
                    0,
                    0,
                    "quiet",
                    0,
                    "",
                    "completed",
                    "pending",
                    "pending",
                    3,
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                ),
            )

    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations)
    initialize_database(db_path)
    initialize_database(db_path)

    with closing(connect_database(db_path)) as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        historical = connection.execute(
            "SELECT * FROM jobs WHERE job_id = 'historical_completed'"
        ).fetchone()

    assert versions == [1, 2, 3, 4, 5, 6]
    assert {
        "reproduce_provider",
        "reproduce_model",
        "evaluation_provider",
        "evaluation_model",
        "evaluation_fallback_models_json",
        "provider_registry_version",
        "provider_contract_fingerprint",
    }.issubset(columns)
    assert historical["execution_status"] == "completed"
    assert historical["reproduce_provider"] is None


def test_repository_persists_selection_snapshot_and_idempotency_keeps_original(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    original = _provider_snapshot()
    changed = _provider_snapshot(
        reproduce_provider="deepseek",
        reproduce_model="deepseek-v4-pro",
        provider_contract_fingerprint="b" * 64,
    )

    first, created = repository.create_job(
        job_id="job_snapshot",
        request=_request(),
        paper_name="paper",
        idempotency_key="snapshot-key",
        provider_snapshot=original,
    )
    replay, replay_created = repository.create_job(
        job_id="job_rebound",
        request=_request(),
        paper_name="paper",
        idempotency_key="snapshot-key",
        provider_snapshot=changed,
    )

    assert created is True
    assert replay_created is False
    assert replay["job_id"] == first["job_id"] == "job_snapshot"
    assert replay["reproduce_provider"] == "openai"
    assert replay["reproduce_model"] == "gpt-4.1-mini"
    assert replay["evaluation_fallback_models"] == ["gpt-4o-mini"]
    assert replay["provider_contract_fingerprint"] == "a" * 64


def test_provider_snapshot_rejects_secrets_base_urls_and_invalid_shape(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    for extra in (
        {"api_key": "UNIQUE-SNAPSHOT-SECRET"},
        {"base_url": "https://must-not-persist.invalid/v1"},
        {"evaluation_fallback_models": ["ok", 1]},
        {"provider_contract_fingerprint": "short"},
    ):
        with pytest.raises(ValueError):
            repository.create_job(
                job_id="job_invalid_snapshot",
                request=_request(),
                paper_name="paper",
                provider_snapshot={**_provider_snapshot(), **extra},
            )

    with closing(connect_database(tmp_path / "paper2code.db")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


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
