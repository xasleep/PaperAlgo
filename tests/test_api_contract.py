import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_api import artifact_service, job_service, log_service, main as main_module, settings_store
from web_api.schemas import WebSettings


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"


def _settings() -> WebSettings:
    return WebSettings(
        reproduce={"provider": "openai", "model": "test-model", "api_key": "repro-secret"},
        evaluation={
            "provider": "openai",
            "model": "test-model",
            "api_key": "eval-secret",
            "fallback_models": ["gpt-4o-mini"],
        },
    )


def _job_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "upload_id": "0" * 32,
        "paper_name": "paper",
        "domain": "statistics",
        "eval_type": "ref_free",
        "generated_n": "1",
        "auto_refine": "false",
        "max_repair_rounds": "0",
        "console_output": "quiet",
        "skip_mineru": "false",
        "pdf_markdown_path": "",
    }
    payload.update(overrides)
    return payload


def _authorize(client: TestClient) -> None:
    session = client.get(f"{API_PREFIX}/session", headers={"Origin": LOCAL_ORIGIN})
    assert session.status_code == 200
    client.headers.update(
        {
            "Origin": LOCAL_ORIGIN,
            "X-CSRF-Token": session.json()["csrf_token"],
        }
    )


def _upload(client: TestClient, name: str, content: bytes):
    return client.post(
        f"{API_PREFIX}/uploads",
        files={"file": (name, content, "application/pdf")},
    )


def _assert_error(
    response,
    *,
    status_code: int,
    code: str,
    message: str | None = None,
    forbidden: list[str] | None = None,
) -> dict:
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert isinstance(body["error"]["details"], dict)
    if message is not None:
        assert body["error"]["message"] == message
    response_text = response.text
    for item in forbidden or []:
        assert item not in response_text
    return body


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def contract_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runs_dir = tmp_path / "runs"
    uploads_dir = tmp_path / ".local" / "uploads"
    settings_path = tmp_path / ".local" / "web_settings.json"
    runs_dir.mkdir(parents=True)

    monkeypatch.setattr(artifact_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(log_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(job_service, "UPLOADS_DIR", uploads_dir)
    monkeypatch.setattr(job_service, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(settings_store, "LOCAL_DIR", settings_path.parent)
    monkeypatch.setattr(settings_store, "SETTINGS_PATH", settings_path)
    job_service.ACTIVE_PROCESSES.clear()
    job_service.ACTIVE_LOG_FILES.clear()

    client = TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    )
    _authorize(client)
    yield client, runs_dir, tmp_path, settings_path

    job_service.ACTIVE_PROCESSES.clear()
    job_service.ACTIVE_LOG_FILES.clear()


def test_settings_not_configured_create_job_returns_structured_error(
    contract_client: tuple[TestClient, Path, Path, Path],
) -> None:
    client, _, _, settings_path = contract_client
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text('{"reproduce": "broken"', encoding="utf-8")

    response = client.post(
        f"{API_PREFIX}/jobs",
        json=_job_payload(),
    )

    _assert_error(
        response,
        status_code=400,
        code="settings_not_configured",
        forbidden=["repro-secret", "eval-secret", str(settings_path.parent)],
    )


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", f"{API_PREFIX}/jobs/missing_job"),
        ("get", f"{API_PREFIX}/jobs/missing_job/logs"),
        ("get", f"{API_PREFIX}/jobs/missing_job/artifacts"),
        ("get", f"{API_PREFIX}/jobs/missing_job/repo/tree"),
        ("get", f"{API_PREFIX}/jobs/missing_job/repo/file?path=main.py"),
        ("get", f"{API_PREFIX}/jobs/missing_job/export"),
        ("post", f"{API_PREFIX}/jobs/missing_job/cancel"),
    ],
)
def test_missing_job_routes_return_job_not_found(
    contract_client: tuple[TestClient, Path, Path, Path],
    method: str,
    url: str,
) -> None:
    client, _, _, _ = contract_client

    response = getattr(client, method)(url)

    _assert_error(response, status_code=404, code="job_not_found")


@pytest.mark.parametrize(
    "url",
    [
        f"{API_PREFIX}/jobs/C:Windows",
        f"{API_PREFIX}/jobs/C:Windows/logs",
        f"{API_PREFIX}/jobs/C:Windows/repo/tree",
    ],
)
def test_invalid_job_id_returns_stable_error(
    contract_client: tuple[TestClient, Path, Path, Path],
    url: str,
) -> None:
    client, _, _, _ = contract_client

    response = client.get(url)

    _assert_error(response, status_code=400, code="invalid_job_id")


@pytest.mark.parametrize(
    ("status_value", "expected_reason"),
    [
        ("completed", "already_finished"),
        ("failed", "already_finished"),
        ("canceled", "already_finished"),
    ],
)
def test_cancel_finished_jobs_returns_conflict(
    contract_client: tuple[TestClient, Path, Path, Path],
    status_value: str,
    expected_reason: str,
) -> None:
    client, runs_dir, _, _ = contract_client
    _write_json(
        runs_dir / "done_job" / "run_status.json",
        {"job_id": "done_job", "status": status_value},
    )

    response = client.post(f"{API_PREFIX}/jobs/done_job/cancel")

    body = _assert_error(response, status_code=409, code="job_not_cancelable")
    assert body["error"]["details"]["reason"] == expected_reason


def test_cancel_detached_and_orphaned_jobs_return_conflict(
    contract_client: tuple[TestClient, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runs_dir, _, _ = contract_client
    detached_pid = 43210
    orphaned_pid = 54321
    _write_json(
        runs_dir / "detached_job" / "run_status.json",
        {"job_id": "detached_job", "status": "running", "process_pid": detached_pid},
    )
    _write_json(
        runs_dir / "orphaned_job" / "run_status.json",
        {"job_id": "orphaned_job", "status": "running", "process_pid": orphaned_pid},
    )
    monkeypatch.setattr(job_service, "is_pid_alive", lambda pid: pid == detached_pid)

    detached = client.post(f"{API_PREFIX}/jobs/detached_job/cancel")
    orphaned = client.post(f"{API_PREFIX}/jobs/orphaned_job/cancel")

    detached_body = _assert_error(detached, status_code=409, code="job_not_cancelable")
    orphaned_body = _assert_error(orphaned, status_code=409, code="job_not_cancelable")
    assert detached_body["error"]["details"]["reason"] == "detached_after_restart"
    assert orphaned_body["error"]["details"]["reason"] == "orphaned_process"


def test_repo_file_contract_errors(
    contract_client: tuple[TestClient, Path, Path, Path],
) -> None:
    client, runs_dir, _, _ = contract_client
    repo_dir = runs_dir / "job1" / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / "big.txt").write_bytes(b"a" * (artifact_service.MAX_TEXT_FILE_BYTES + 1))
    (repo_dir / "binary.txt").write_bytes(b"text\x00binary")
    (repo_dir / "data.bin").write_bytes(b"plain text but disallowed")
    (repo_dir / "latin1.txt").write_bytes(b"\xff\xfeinvalid")

    traversal = client.get(f"{API_PREFIX}/jobs/job1/repo/file", params={"path": "../secret.txt"})
    too_large = client.get(f"{API_PREFIX}/jobs/job1/repo/file", params={"path": "big.txt"})
    binary = client.get(f"{API_PREFIX}/jobs/job1/repo/file", params={"path": "binary.txt"})
    unsupported_ext = client.get(f"{API_PREFIX}/jobs/job1/repo/file", params={"path": "data.bin"})
    invalid_utf8 = client.get(f"{API_PREFIX}/jobs/job1/repo/file", params={"path": "latin1.txt"})

    _assert_error(traversal, status_code=400, code="invalid_repo_path")
    _assert_error(too_large, status_code=413, code="file_too_large")
    _assert_error(binary, status_code=415, code="binary_file_not_supported")
    _assert_error(unsupported_ext, status_code=415, code="unsupported_file_type")
    _assert_error(invalid_utf8, status_code=415, code="unsupported_file_type")


def test_logs_semantics_are_stable(
    contract_client: tuple[TestClient, Path, Path, Path],
) -> None:
    client, runs_dir, _, _ = contract_client
    (runs_dir / "job_without_logs").mkdir()
    logs_dir = runs_dir / "job_with_logs" / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "run.log").write_text("hello\nworld\n", encoding="utf-8")

    no_logs = client.get(f"{API_PREFIX}/jobs/job_without_logs/logs")
    missing_log = client.get(f"{API_PREFIX}/jobs/job_with_logs/logs", params={"file": "missing.log"})
    invalid_log = client.get(f"{API_PREFIX}/jobs/job_with_logs/logs", params={"file": "../secret.log"})

    assert no_logs.status_code == 200
    assert no_logs.json()["logs"] == []
    _assert_error(missing_log, status_code=404, code="log_not_found")
    _assert_error(invalid_log, status_code=400, code="invalid_repo_path")


def test_artifacts_without_generated_outputs_return_200(
    contract_client: tuple[TestClient, Path, Path, Path],
) -> None:
    client, runs_dir, _, _ = contract_client
    (runs_dir / "job1").mkdir()

    response = client.get(f"{API_PREFIX}/jobs/job1/artifacts")

    assert response.status_code == 200
    body = response.json()
    assert body["repo_ready"] is False
    assert body["repo_file_count"] == 0
    assert body["result_file_count"] == 0
    assert body["log_file_count"] == 0


def test_jobs_route_is_not_captured_by_dynamic_job_route(
    contract_client: tuple[TestClient, Path, Path, Path],
) -> None:
    client, runs_dir, _, _ = contract_client
    _write_json(
        runs_dir / "job1" / "run_status.json",
        {"job_id": "job1", "status": "running", "paper_name": "paper"},
    )

    response = client.get(f"{API_PREFIX}/jobs")

    assert response.status_code == 200
    assert "jobs" in response.json()
    assert response.json()["jobs"][0]["job_id"] == "job1"


def test_jobs_and_job_detail_use_stable_status_fields(
    contract_client: tuple[TestClient, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runs_dir, _, _ = contract_client
    detached_pid = 11111
    _write_json(
        runs_dir / "queued_job" / "run_status.json",
        {"job_id": "queued_job", "status": "starting"},
    )
    _write_json(
        runs_dir / "detached_job" / "run_status.json",
        {"job_id": "detached_job", "status": "running", "process_pid": detached_pid},
    )
    monkeypatch.setattr(job_service, "is_pid_alive", lambda pid: pid == detached_pid)

    list_response = client.get(f"{API_PREFIX}/jobs")
    detail_response = client.get(f"{API_PREFIX}/jobs/detached_job")

    assert list_response.status_code == 200
    jobs = {job["job_id"]: job for job in list_response.json()["jobs"]}
    assert jobs["queued_job"]["status"] == "queued"
    assert jobs["queued_job"]["process_state"] == "none"
    assert jobs["queued_job"]["cancelable"] is False
    assert jobs["queued_job"]["cancel_unavailable_reason"] == "process_not_registered"

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["status"] == "running"
    assert detail["process_state"] == "detached"
    assert detail["cancelable"] is False
    assert detail["cancel_unavailable_reason"] == "detached_after_restart"


def test_post_jobs_upload_and_parameter_errors_are_stable(
    contract_client: tuple[TestClient, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runs_dir, tmp_path, _ = contract_client
    monkeypatch.setattr(main_module, "load_settings", _settings)

    not_pdf = _upload(client, "paper.txt", b"%PDF-1.7\nbody")
    fake_pdf = _upload(client, "paper.pdf", b"not a pdf")
    valid_upload = _upload(client, "paper.pdf", b"%PDF-1.7\nbody")
    upload_id = valid_upload.json()["upload_id"]
    bad_generated_n = client.post(
        f"{API_PREFIX}/jobs",
        json=_job_payload(upload_id=upload_id, generated_n=33),
    )
    missing_markdown = client.post(
        f"{API_PREFIX}/jobs",
        json=_job_payload(upload_id=upload_id, skip_mineru=True),
    )
    external_markdown = tmp_path / "outside.md"
    external_markdown.write_text("# outside", encoding="utf-8")
    bad_markdown_path = client.post(
        f"{API_PREFIX}/jobs",
        json=_job_payload(
            upload_id=upload_id,
            skip_mineru=True,
            pdf_markdown_path=str(external_markdown),
        ),
    )

    _assert_error(not_pdf, status_code=415, code="unsupported_file_type")
    _assert_error(fake_pdf, status_code=400, code="invalid_upload")
    generated_body = _assert_error(bad_generated_n, status_code=400, code="invalid_parameter")
    missing_markdown_body = _assert_error(
        missing_markdown,
        status_code=400,
        code="invalid_parameter",
    )
    bad_markdown_body = _assert_error(
        bad_markdown_path,
        status_code=400,
        code="invalid_parameter",
    )
    assert generated_body["error"]["details"]["parameter"] == "generated_n"
    assert missing_markdown_body["error"]["details"]["parameter"] == "pdf_markdown_path"
    assert bad_markdown_body["error"]["details"]["parameter"] == "pdf_markdown_path"


def test_repo_download_internal_errors_do_not_leak_paths_or_secrets(
    contract_client: tuple[TestClient, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runs_dir, tmp_path, _ = contract_client
    (runs_dir / "job1" / "repo").mkdir(parents=True)
    secret = "very-secret-api-key"
    sensitive_path = str(tmp_path / "private" / "secret")

    def broken_zip(job_id: str) -> Path:
        raise OSError(f"{sensitive_path} :: {secret}")

    monkeypatch.setattr(main_module, "make_repo_zip", broken_zip)

    response = client.get(f"{API_PREFIX}/jobs/job1/export")

    _assert_error(
        response,
        status_code=500,
        code="internal_error",
        message="Internal server error.",
        forbidden=[secret, sensitive_path, "private"],
    )


def test_repo_missing_returns_stable_codes(
    contract_client: tuple[TestClient, Path, Path, Path],
) -> None:
    client, runs_dir, _, _ = contract_client
    (runs_dir / "job1").mkdir()

    file_response = client.get(f"{API_PREFIX}/jobs/job1/repo/file", params={"path": "main.py"})
    download_response = client.get(f"{API_PREFIX}/jobs/job1/export")

    _assert_error(file_response, status_code=404, code="repo_not_available")
    _assert_error(download_response, status_code=404, code="repo_not_available")
