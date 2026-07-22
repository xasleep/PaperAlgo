from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_api import job_service, main as main_module
from web_api.database import connect_database, normalize_database_path
from web_api.job_repository import JobRepository
from web_api.schemas import WebSettings


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"


def _authorize(client: TestClient) -> dict[str, str]:
    session = client.get(f"{API_PREFIX}/session", headers={"Origin": LOCAL_ORIGIN})
    assert session.status_code == 200
    return {
        "Origin": LOCAL_ORIGIN,
        "X-CSRF-Token": session.json()["csrf_token"],
    }


def _settings(reproduce_secret: str, evaluation_secret: str) -> WebSettings:
    return WebSettings(
        reproduce={
            "provider": "openai",
            "model": "fake-model",
            "api_key": reproduce_secret,
        },
        evaluation={
            "provider": "openai",
            "model": "fake-model",
            "api_key": evaluation_secret,
            "fallback_models": [],
        },
    )


def _payload(upload_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "upload_id": upload_id,
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
    payload.update(overrides)
    return payload


def test_sqlite_runtime_is_idempotent_persistent_and_never_starts_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / ".local" / "paper2code.db"
    uploads_dir = tmp_path / ".local" / "uploads"
    runs_dir = tmp_path / "runs"
    reproduce_secret = "REPRODUCE-" + "DB-SECRET"
    evaluation_secret = "EVALUATION-" + "DB-SECRET"
    monkeypatch.setenv("JOB_RUNTIME", "sqlite")
    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(db_path))
    monkeypatch.setattr(job_service, "UPLOADS_DIR", uploads_dir)
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(
        main_module,
        "load_settings",
        lambda: _settings(reproduce_secret, evaluation_secret),
    )
    start_calls: list[str] = []
    monkeypatch.setattr(
        main_module,
        "start_job",
        lambda **kwargs: start_calls.append("start"),
    )
    repository_constructions: list[Path] = []

    class RecordingJobRepository(JobRepository):
        def __init__(self, database_path: Path | str | None = None) -> None:
            repository_constructions.append(normalize_database_path(database_path))
            super().__init__(database_path)

    monkeypatch.setattr(main_module, "JobRepository", RecordingJobRepository)

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("sqlite runtime must not call subprocess.Popen")

    monkeypatch.setattr(job_service.subprocess, "Popen", forbidden_popen)

    with TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    ) as first_client:
        headers = _authorize(first_client)
        upload = first_client.post(
            f"{API_PREFIX}/uploads",
            headers=headers,
            files={"file": ("paper.pdf", b"%PDF-test", "application/pdf")},
        )
        assert upload.status_code == 200
        payload = _payload(upload.json()["upload_id"])
        create_headers = {**headers, "Idempotency-Key": "stable-create-key"}

        first = first_client.post(
            f"{API_PREFIX}/jobs",
            headers=create_headers,
            json=payload,
        )
        monkeypatch.setattr(main_module, "load_settings", lambda: None)
        (uploads_dir / payload["upload_id"] / "document.pdf").unlink()
        replay = first_client.post(
            f"{API_PREFIX}/jobs",
            headers=create_headers,
            json=payload,
        )
        conflict = first_client.post(
            f"{API_PREFIX}/jobs",
            headers=create_headers,
            json={**payload, "paper_name": "different"},
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert first.json()["job_id"] == replay.json()["job_id"]
    assert first.json()["execution_status"] == "queued"
    assert first.json()["evaluation_status"] == "pending"
    assert first.json()["quality_status"] == "pending"
    assert start_calls == []
    assert not runs_dir.exists()

    with TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    ) as restarted_client:
        listed = restarted_client.get(f"{API_PREFIX}/jobs")
        detail = restarted_client.get(f"{API_PREFIX}/jobs/{first.json()['job_id']}")

    assert listed.status_code == 200
    assert [job["job_id"] for job in listed.json()["jobs"]] == [
        first.json()["job_id"]
    ]
    assert detail.status_code == 200
    assert detail.json()["job_id"] == first.json()["job_id"]
    assert detail.json()["execution_status"] == "queued"
    assert detail.json()["version"] == 1
    assert repository_constructions == [normalize_database_path(db_path)]

    with closing(connect_database(db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        stored_key_hash = connection.execute(
            "SELECT idempotency_key_hash FROM jobs"
        ).fetchone()[0]
    assert stored_key_hash != "stable-create-key"
    for database_file in db_path.parent.glob("paper2code.db*"):
        contents = database_file.read_bytes()
        if reproduce_secret.encode() in contents:
            pytest.fail("SQLite files contain the reproduce API credential", pytrace=False)
        if evaluation_secret.encode() in contents:
            pytest.fail("SQLite files contain the evaluation API credential", pytrace=False)


def test_sqlite_cancel_is_idempotent_command_without_legacy_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / ".local" / "paper2code.db"
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("JOB_RUNTIME", "sqlite")
    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(db_path))
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(db_path)
    created, _ = repository.create_job(
        job_id="sqlite_cancel_job",
        request=_payload("0" * 32),
        paper_name="paper",
    )
    legacy_calls: list[str] = []

    def forbidden_legacy_cancel(job_id: str):
        legacy_calls.append(job_id)
        raise AssertionError("sqlite cancel must not enter the legacy runtime")

    monkeypatch.setattr(main_module, "cancel_job", forbidden_legacy_cancel)
    with closing(connect_database(db_path)) as connection:
        events_before = connection.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id = ?",
            (created["job_id"],),
        ).fetchone()[0]

    with TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        headers = _authorize(client)
        first = client.post(
            f"{API_PREFIX}/jobs/{created['job_id']}/cancel",
            headers=headers,
        )
        repeated = client.post(
            f"{API_PREFIX}/jobs/{created['job_id']}/cancel",
            headers=headers,
        )
        missing = client.post(
            f"{API_PREFIX}/jobs/missing_sqlite_job/cancel",
            headers=headers,
        )

    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json() == {
        "job_id": created["job_id"],
        "canceled": False,
        "message": "Cancellation requested.",
    }
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "job_not_found"
    assert legacy_calls == []
    assert repository.get_job(created["job_id"]) == created
    assert not runs_dir.exists()
    with closing(connect_database(db_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id = ?",
            (created["job_id"],),
        ).fetchone()[0] == events_before
        command = connection.execute(
            "SELECT command_type, status FROM job_commands WHERE job_id = ?",
            (created["job_id"],),
        ).fetchone()
        assert tuple(command) == ("cancel", "pending")
        assert connection.execute(
            "SELECT COUNT(*) FROM job_commands WHERE job_id = ?",
            (created["job_id"],),
        ).fetchone()[0] == 1


def test_sqlite_unresolved_launch_is_detached_redacted_and_not_cancelable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / ".local" / "paper2code.db"
    monkeypatch.setenv("JOB_RUNTIME", "sqlite")
    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(db_path))
    monkeypatch.setattr(job_service, "RUNS_DIR", tmp_path / "runs")
    repository = JobRepository(db_path)
    repository.create_job(
        job_id="unresolved_job",
        request=_payload("0" * 32),
        paper_name="paper",
    )
    assert repository.acquire_worker_lease("worker", "instance-secret") is True
    repository.claim_next_queued_job(
        worker_id="worker",
        instance_token="instance-secret",
        launch_token="launch-secret",
    )
    repository.mark_launch_identity_unresolved(
        "unresolved_job",
        worker_id="worker",
        instance_token="instance-secret",
        launch_token="launch-secret",
    )
    assert repository.release_worker_lease("worker", "instance-secret") is True
    legacy_calls: list[str] = []

    def forbidden_legacy_cancel(job_id: str):
        legacy_calls.append(job_id)
        raise AssertionError("sqlite unresolved cancel must not enter legacy runtime")

    monkeypatch.setattr(main_module, "cancel_job", forbidden_legacy_cancel)
    with TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        headers = _authorize(client)
        listed = client.get(f"{API_PREFIX}/jobs")
        detail = client.get(f"{API_PREFIX}/jobs/unresolved_job")
        canceled = client.post(
            f"{API_PREFIX}/jobs/unresolved_job/cancel",
            headers=headers,
        )

    assert listed.status_code == detail.status_code == 200
    list_item = listed.json()["jobs"][0]
    for view in (list_item, detail.json()):
        assert view["status"] == "running"
        assert view["process_state"] == "detached"
        assert view["process_active"] is False
        assert view["cancelable"] is False
        assert view["cancel_unavailable_reason"] == "process_identity_unresolved"
        assert view["message"] == (
            "Pipeline launch identity is unresolved; new work is blocked for safety."
        )
        for forbidden_field in (
            "pid",
            "process_create_time",
            "process_group_id",
            "launch_token",
            "instance_token",
            "command_summary",
        ):
            assert forbidden_field not in view
    assert canceled.status_code == 409
    assert canceled.json()["error"]["code"] == "job_not_cancelable"
    assert canceled.json()["error"]["details"] == {
        "reason": "process_identity_unresolved"
    }
    assert repository.get_job("unresolved_job")["execution_status"] == "running"
    assert repository.get_cancel_command("unresolved_job") is None
    assert legacy_calls == []


def test_sqlite_recovery_status_is_limited_and_redacts_process_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / ".local" / "paper2code.db"
    monkeypatch.setenv("JOB_RUNTIME", "sqlite")
    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(db_path))
    monkeypatch.setattr(job_service, "RUNS_DIR", tmp_path / "runs")
    repository = JobRepository(db_path)
    repository.create_job(
        job_id="recovery_view",
        request=_payload("0" * 32),
        paper_name="paper",
    )
    assert repository.acquire_worker_lease("worker", "instance-secret")
    repository.claim_next_queued_job(
        worker_id="worker",
        instance_token="instance-secret",
        launch_token="old-launch-secret",
    )
    repository.record_process_started(
        "recovery_view",
        worker_id="worker",
        instance_token="instance-secret",
        launch_token="old-launch-secret",
        pid=12345,
        process_create_time="create-12345",
        process_group_id=12345,
        command_summary="python run_pipeline.py --job-id redacted",
    )
    repository.record_completed_checkpoint(
        "recovery_view",
        worker_id="worker",
        instance_token="instance-secret",
        launch_token="old-launch-secret",
        checkpoint={
            "version": 1,
            "stage_name": "planning",
            "stage_sequence": 2,
            "stage_attempt": 2,
            "status": "completed",
            "resume_from_stage": "extract_config",
        },
        checkpoint_path="checkpoints/checkpoint-0002-planning-002.json",
    )
    repository.prepare_recovery_attempt(
        "recovery_view",
        worker_id="worker",
        instance_token="instance-secret",
        launch_token="old-launch-secret",
        new_launch_token="new-launch-secret",
        resume_from_stage="extract_config",
        resume_stage_sequence=3,
        resume_stage_attempt=3,
    )
    assert repository.release_worker_lease("worker", "instance-secret")

    with TestClient(main_module.app, base_url=LOCAL_ORIGIN) as client:
        response = client.get(f"{API_PREFIX}/jobs/recovery_view")

    assert response.status_code == 200
    view = response.json()
    assert view["status"] == "running"
    assert view["stage"] == "extract_config"
    assert view["current_stage"] == "extract_config"
    assert view["stage_attempt"] == 3
    assert view["last_checkpoint_stage"] == "planning"
    assert view["recovery_count"] == 1
    assert view["recovery_status"] == "prepared"
    assert view["recovery_error_code"] is None
    assert view["message"] == (
        "Pipeline recovery is starting from the last verified stage boundary."
    )
    for forbidden_field in (
        "pid",
        "process_create_time",
        "process_group_id",
        "launch_token",
        "instance_token",
        "command_summary",
        "checkpoint_path",
    ):
        assert forbidden_field not in view


def test_legacy_runtime_keeps_existing_start_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    uploads_dir = tmp_path / ".local" / "uploads"
    monkeypatch.setenv("JOB_RUNTIME", "legacy")
    monkeypatch.setattr(job_service, "UPLOADS_DIR", uploads_dir)
    monkeypatch.setattr(main_module, "load_settings", lambda: _settings("fake", "fake"))
    calls: list[str] = []

    def fake_start_job(**kwargs):
        calls.append(kwargs["job_id"])
        run_dir = tmp_path / "runs" / kwargs["job_id"]
        return {
            "job_id": kwargs["job_id"],
            "status": "running",
            "run_dir": str(run_dir),
            "status_path": str(run_dir / "run_status.json"),
            "summary_path": str(run_dir / "run_summary.json"),
        }

    monkeypatch.setattr(main_module, "start_job", fake_start_job)
    with TestClient(main_module.app, base_url=LOCAL_ORIGIN) as client:
        headers = _authorize(client)
        upload = client.post(
            f"{API_PREFIX}/uploads",
            headers=headers,
            files={"file": ("paper.pdf", b"%PDF-test", "application/pdf")},
        )
        response = client.post(
            f"{API_PREFIX}/jobs",
            headers={**headers, "Idempotency-Key": "legacy-ignored"},
            json=_payload(upload.json()["upload_id"]),
        )

    assert response.status_code == 200
    assert len(calls) == 1
