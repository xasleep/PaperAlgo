from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_api import job_service, main as main_module
from web_api.database import connect_database
from web_api.job_repository import JobRepository
from web_api.worker import PipelineWorker


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"
EVIL_ORIGIN = "http://evil.example"


def _request() -> dict[str, object]:
    return {
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


def _authorize(client: TestClient) -> dict[str, str]:
    session = client.get(f"{API_PREFIX}/session", headers={"Origin": LOCAL_ORIGIN})
    assert session.status_code == 200
    return {
        "Origin": LOCAL_ORIGIN,
        "X-CSRF-Token": session.json()["csrf_token"],
    }


def _fail_job(repository: JobRepository, job_id: str) -> None:
    repository.create_job(job_id=job_id, request=_request(), paper_name="paper")
    repository.transition_job(
        job_id,
        expected_version=1,
        execution_status="failed",
        evaluation_status="skipped",
        quality_status="skipped",
    )


def _complete_rejected_job(repository: JobRepository, job_id: str) -> None:
    repository.create_job(job_id=job_id, request=_request(), paper_name="paper")
    repository.transition_job(
        job_id,
        expected_version=1,
        execution_status="running",
    )
    repository.transition_job(
        job_id,
        expected_version=2,
        execution_status="completed",
        evaluation_status="completed",
        quality_status="rejected",
    )


@pytest.fixture()
def command_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / ".local" / "paper2code.db"
    monkeypatch.setenv("JOB_RUNTIME", "sqlite")
    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(db_path))
    monkeypatch.setattr(job_service, "RUNS_DIR", tmp_path / "runs")
    main_module._SQLITE_REPOSITORIES.clear()
    repository = JobRepository(db_path)
    with TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        headers = _authorize(client)
        yield client, headers, repository, db_path
    main_module._SQLITE_REPOSITORIES.clear()


def test_command_post_is_idempotent_and_queryable(
    command_client: tuple[TestClient, dict[str, str], JobRepository, Path],
) -> None:
    client, headers, repository, db_path = command_client
    repository.create_job(job_id="command_cancel", request=_request(), paper_name="paper")

    first = client.post(
        f"{API_PREFIX}/jobs/command_cancel/commands",
        headers={**headers, "Idempotency-Key": "same-command-request"},
        json={"command_type": "cancel"},
    )
    repeated = client.post(
        f"{API_PREFIX}/jobs/command_cancel/commands",
        headers={**headers, "Idempotency-Key": "same-command-request"},
        json={"command_type": "cancel"},
    )
    command_id = first.json()["command_id"]
    fetched = client.get(
        f"{API_PREFIX}/jobs/command_cancel/commands/{command_id}",
        headers={"Origin": LOCAL_ORIGIN},
    )
    listed = client.get(
        f"{API_PREFIX}/jobs/command_cancel/commands",
        headers={"Origin": LOCAL_ORIGIN},
    )

    assert first.status_code == repeated.status_code == fetched.status_code == 200
    assert first.json() == repeated.json() == fetched.json()
    assert first.json()["status"] == "pending"
    assert first.json()["request_status"] == "accepted"
    assert first.json()["command_type"] == "cancel"
    assert [item["command_id"] for item in listed.json()["commands"]] == [command_id]
    assert "same-command-request" not in first.text + repeated.text + fetched.text
    with closing(connect_database(db_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_commands WHERE job_id = ?",
            ("command_cancel",),
        ).fetchone()[0] == 1


def test_command_api_rejects_missing_idempotency_key_before_persisting(
    command_client: tuple[TestClient, dict[str, str], JobRepository, Path],
) -> None:
    client, headers, repository, db_path = command_client
    repository.create_job(job_id="missing_key", request=_request(), paper_name="paper")

    response = client.post(
        f"{API_PREFIX}/jobs/missing_key/commands",
        headers=headers,
        json={"command_type": "cancel"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_parameter"
    with closing(connect_database(db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_commands").fetchone()[0] == 0


def test_rejected_command_has_stable_queryable_reason(
    command_client: tuple[TestClient, dict[str, str], JobRepository, Path],
) -> None:
    client, headers, repository, _ = command_client
    _complete_rejected_job(repository, "finished_job")

    response = client.post(
        f"{API_PREFIX}/jobs/finished_job/commands",
        headers={**headers, "Idempotency-Key": "cancel-finished"},
        json={"command_type": "cancel"},
    )
    command_id = response.json()["error"]["details"]["command_id"]
    fetched = client.get(
        f"{API_PREFIX}/jobs/finished_job/commands/{command_id}",
        headers={"Origin": LOCAL_ORIGIN},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_command_rejected"
    assert response.json()["error"]["details"]["reason"] == "already_finished"
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "rejected"
    assert fetched.json()["request_status"] == "rejected"
    assert fetched.json()["error_code"] == "already_finished"


def test_identity_unresolved_rejects_every_conflicting_command(
    command_client: tuple[TestClient, dict[str, str], JobRepository, Path],
) -> None:
    client, headers, repository, _ = command_client
    repository.create_job(job_id="identity_blocked", request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("worker", "token")
    repository.claim_next_queued_job(
        worker_id="worker",
        instance_token="token",
        launch_token="launch-token",
    )
    repository.mark_launch_identity_unresolved(
        "identity_blocked",
        worker_id="worker",
        instance_token="token",
        launch_token="launch-token",
    )
    assert repository.release_worker_lease("worker", "token")

    responses = [
        client.post(
            f"{API_PREFIX}/jobs/identity_blocked/commands",
            headers={**headers, "Idempotency-Key": f"identity-{command_type}"},
            json={"command_type": command_type},
        )
        for command_type in ("cancel", "retry", "repair", "approve")
    ]

    assert [response.status_code for response in responses] == [409, 409, 409, 409]
    assert {
        response.json()["error"]["details"]["reason"] for response in responses
    } == {"process_identity_unresolved"}
    assert repository.get_job("identity_blocked")["execution_status"] == "running"
    with closing(connect_database(repository.database_path)) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM job_commands
            WHERE job_id = ? AND status = 'rejected'
            """,
            ("identity_blocked",),
        ).fetchone()[0] == 4


def test_worker_requires_lease_before_applying_retry_command(
    command_client: tuple[TestClient, dict[str, str], JobRepository, Path],
) -> None:
    _, _, repository, _ = command_client
    _fail_job(repository, "retry_needs_lease")
    command = repository.request_job_command(
        "retry_needs_lease",
        command_type="retry",
        idempotency_key="retry-key",
    )

    with pytest.raises(RuntimeError, match="lease"):
        repository.apply_next_retry_command(
            worker_id="worker",
            instance_token="token",
        )

    assert repository.get_job("retry_needs_lease")["execution_status"] == "failed"
    assert repository.get_job_command("retry_needs_lease", command["id"])["status"] == (
        "pending"
    )


def test_worker_retry_requeues_once_and_cancel_precedence_prevents_launch(
    command_client: tuple[TestClient, dict[str, str], JobRepository, Path],
) -> None:
    _, _, repository, _ = command_client
    _fail_job(repository, "retry_then_cancel")
    repository.request_job_command(
        "retry_then_cancel",
        command_type="retry",
        idempotency_key="retry-then-cancel",
    )
    worker = PipelineWorker(
        repository=repository,
        worker_id="retry-worker",
        instance_token="retry-token",
        command_builder=lambda job: (_ for _ in ()).throw(
            AssertionError("queued cancel must be consumed before retry launch")
        ),
    )

    try:
        assert repository.acquire_worker_lease(
            worker.worker_id,
            worker.instance_token,
            lease_seconds=worker.lease_seconds,
        )
        worker._lease_owned = True
        applied = repository.apply_next_retry_command(
            worker_id=worker.worker_id,
            instance_token=worker.instance_token,
        )
        assert applied is not None
        repository.request_cancel("retry_then_cancel")
        assert worker.run_once() is True
    finally:
        worker.close()

    job = repository.get_job("retry_then_cancel")
    assert job["execution_status"] == "canceled"
    assert repository.get_cancel_command("retry_then_cancel")["status"] == "completed"
    commands = repository.list_job_commands("retry_then_cancel")
    assert [(item["command_type"], item["status"]) for item in commands] == [
        ("retry", "completed"),
        ("cancel", "completed"),
    ]


def test_approve_and_repair_commands_are_ordered_and_idempotent(
    command_client: tuple[TestClient, dict[str, str], JobRepository, Path],
) -> None:
    _, _, repository, _ = command_client
    _complete_rejected_job(repository, "approve_race")
    approve = repository.request_job_command(
        "approve_race",
        command_type="approve",
        idempotency_key="approve-first",
    )
    repair = repository.request_job_command(
        "approve_race",
        command_type="repair",
        idempotency_key="repair-second",
    )
    worker = PipelineWorker(
        repository=repository,
        worker_id="approve-worker",
        instance_token="approve-token",
    )

    try:
        assert repository.acquire_worker_lease(
            worker.worker_id,
            worker.instance_token,
            lease_seconds=worker.lease_seconds,
        )
        worker._lease_owned = True
        approved = repository.apply_next_approve_command(
            worker_id=worker.worker_id,
            instance_token=worker.instance_token,
        )
        repeated = repository.apply_next_approve_command(
            worker_id=worker.worker_id,
            instance_token=worker.instance_token,
        )
        failed_repair = repository.claim_next_repair_command(
            worker_id=worker.worker_id,
            instance_token=worker.instance_token,
        )
    finally:
        worker.close()

    assert approved is not None
    assert repeated is None
    assert failed_repair is None
    assert repository.get_job("approve_race")["quality_status"] == "accepted"
    assert repository.get_job_command("approve_race", approve["id"])["status"] == (
        "completed"
    )
    assert repository.get_job_command("approve_race", repair["id"])["status"] == (
        "failed"
    )
    assert repository.get_job_command("approve_race", repair["id"])["error_code"] == (
        "state_changed"
    )


def test_worker_applies_non_cancel_commands_in_persisted_id_order(
    command_client: tuple[TestClient, dict[str, str], JobRepository, Path],
) -> None:
    _, _, repository, _ = command_client
    _complete_rejected_job(repository, "ordered_commands")
    repair = repository.request_job_command(
        "ordered_commands",
        command_type="repair",
        idempotency_key="repair-first",
    )
    approve = repository.request_job_command(
        "ordered_commands",
        command_type="approve",
        idempotency_key="approve-second",
    )
    worker = PipelineWorker(
        repository=repository,
        worker_id="ordered-worker",
        instance_token="ordered-token",
    )

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    commands = repository.list_job_commands("ordered_commands")
    assert [(item["command_id"], item["command_type"], item["status"]) for item in commands] == [
        (repair["id"], "repair", "completed"),
        (approve["id"], "approve", "completed"),
    ]
    assert repository.get_job("ordered_commands")["quality_status"] == "accepted"


def test_command_security_rejects_cross_site_before_repository_access(
    command_client: tuple[TestClient, dict[str, str], JobRepository, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, headers, _, _ = command_client
    repository_calls: list[str] = []

    def forbidden_repository() -> None:
        repository_calls.append("repository")
        raise AssertionError("security must reject before repository access")

    monkeypatch.setattr(main_module, "_sqlite_repository", forbidden_repository)
    response = client.post(
        f"{API_PREFIX}/jobs/security_job/commands",
        headers={
            **headers,
            "Origin": EVIL_ORIGIN,
            "Idempotency-Key": "evil-command",
        },
        json={"command_type": "cancel"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert repository_calls == []
