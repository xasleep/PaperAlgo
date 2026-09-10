from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_api import main as main_module
from web_api.database import connect_database, utc_now
from web_api.job_repository import JobRepository


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"


def _authorize(client: TestClient) -> dict[str, str]:
    session = client.get(f"{API_PREFIX}/session", headers={"Origin": LOCAL_ORIGIN})
    assert session.status_code == 200
    return {
        "Origin": LOCAL_ORIGIN,
        "X-CSRF-Token": session.json()["csrf_token"],
    }


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "upload_id": "upload",
        "paper_name": "paper",
        "domain": "statistics",
        "eval_type": "ref_free",
        "generated_n": 1,
        "auto_refine": False,
        "max_repair_rounds": 0,
        "console_output": "quiet",
        "skip_mineru": True,
        "pdf_markdown_path": "paper.md",
        "cost_budget_policy": "none",
        "cost_budget_currency": None,
        "cost_budget_amount": None,
    }
    payload.update(overrides)
    return payload


def _create_job(repository: JobRepository, job_id: str) -> dict[str, object]:
    job, _ = repository.create_job(
        job_id=job_id,
        request=_payload(paper_name=job_id),
        paper_name=job_id,
        provider_snapshot={
            "reproduce_provider": "openai",
            "reproduce_model": "gpt-4.1-mini",
            "evaluation_provider": "openai",
            "evaluation_model": "gpt-4.1-mini",
            "evaluation_fallback_models": [],
            "provider_registry_version": 1,
            "provider_contract_fingerprint": "a" * 64,
        },
    )
    return job


def _set_job_state(
    repository: JobRepository,
    job_id: str,
    *,
    execution_status: str,
    recovery_status: str = "none",
) -> None:
    now = utc_now()
    with connect_database(repository.database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE jobs
            SET execution_status = ?,
                recovery_status = ?,
                updated_at = ?,
                version = version + 1
            WHERE job_id = ?
            """,
            (execution_status, recovery_status, now, job_id),
        )
        connection.commit()


@pytest.fixture()
def stop_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / ".local" / "paper2code.db"
    monkeypatch.setenv("JOB_RUNTIME", "sqlite")
    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(db_path))
    main_module._SQLITE_REPOSITORIES.clear()
    calls: list[str] = []
    monkeypatch.setattr(main_module, "launch_stop_script", lambda: calls.append("stop"))
    repository = JobRepository(db_path)
    with TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        yield client, repository, calls
    main_module._SQLITE_REPOSITORIES.clear()


def test_stop_empty_database_returns_202_and_schedules_once(stop_client) -> None:
    client, _repository, calls = stop_client
    response = client.post(f"{API_PREFIX}/system/stop", headers=_authorize(client))

    assert response.status_code == 202
    assert response.json() == {
        "status": "stopping",
        "message": "PaperAlgo is stopping.",
    }
    assert calls == ["stop"]


def test_stop_with_terminal_jobs_returns_202_and_schedules_once(stop_client) -> None:
    client, repository, calls = stop_client
    for status in ("completed", "failed", "canceled"):
        job_id = f"{status}_job"
        _create_job(repository, job_id)
        _set_job_state(repository, job_id, execution_status=status)

    response = client.post(f"{API_PREFIX}/system/stop", headers=_authorize(client))

    assert response.status_code == 202
    assert calls == ["stop"]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_stop_rejects_active_execution_statuses(stop_client, status: str) -> None:
    client, repository, calls = stop_client
    _create_job(repository, f"{status}_job")
    _set_job_state(repository, f"{status}_job", execution_status=status)

    response = client.post(f"{API_PREFIX}/system/stop", headers=_authorize(client))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "system_stop_blocked"
    assert response.json()["error"]["details"]["jobs"][0]["execution_status"] == status
    assert calls == []


@pytest.mark.parametrize("recovery_status", ["prepared", "running"])
def test_stop_rejects_active_recovery_statuses(
    stop_client,
    recovery_status: str,
) -> None:
    client, repository, calls = stop_client
    _create_job(repository, f"recovery_{recovery_status}_job")
    _set_job_state(
        repository,
        f"recovery_{recovery_status}_job",
        execution_status="failed",
        recovery_status=recovery_status,
    )

    response = client.post(f"{API_PREFIX}/system/stop", headers=_authorize(client))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "system_stop_blocked"
    assert response.json()["error"]["details"]["jobs"][0]["recovery_status"] == recovery_status
    assert calls == []


def test_stop_rejects_non_sqlite_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JOB_RUNTIME", "legacy")
    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(tmp_path / ".local" / "paper2code.db"))
    main_module._SQLITE_REPOSITORIES.clear()
    calls: list[str] = []
    monkeypatch.setattr(main_module, "launch_stop_script", lambda: calls.append("stop"))
    with TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        response = client.post(f"{API_PREFIX}/system/stop", headers=_authorize(client))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "feature_not_supported"
    assert calls == []


def test_stop_requires_existing_csrf_boundary(stop_client) -> None:
    client, _repository, calls = stop_client
    response = client.post(
        f"{API_PREFIX}/system/stop",
        headers={"Origin": LOCAL_ORIGIN},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert calls == []
