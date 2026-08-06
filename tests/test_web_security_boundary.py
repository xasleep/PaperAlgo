import asyncio
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette import formparsers
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from web_api import artifact_service, job_service, log_service, main as main_module
from web_api import settings_store, web_security
from web_api.errors import FileTooLargeError
from web_api.schemas import WebSettings


LOCAL_ORIGIN = "http://localhost"
VITE_ORIGIN = "http://localhost:5173"
EVIL_ORIGIN = "http://evil.example"
API_PREFIX = "/api/v1"


def _settings_payload() -> dict[str, object]:
    return {
        "reproduce": {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "api_key": "reproduce-secret",
            "base_url": "https://reproduce.invalid/v1",
        },
        "evaluation": {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "api_key": "evaluation-secret",
            "base_url": "https://evaluation.invalid/v1",
            "fallback_models": [],
        },
    }


@pytest.fixture()
def secure_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runs_dir = tmp_path / "runs"
    uploads_dir = tmp_path / ".local" / "uploads"
    settings_path = tmp_path / ".local" / "web_settings.json"
    runs_dir.mkdir()

    monkeypatch.setattr(artifact_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(log_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(job_service, "UPLOADS_DIR", uploads_dir)
    monkeypatch.setattr(job_service, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(settings_store, "LOCAL_DIR", settings_path.parent)
    monkeypatch.setattr(settings_store, "SETTINGS_PATH", settings_path)

    client = TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    )
    yield client, runs_dir, uploads_dir

    job_service.ACTIVE_PROCESSES.clear()
    job_service.ACTIVE_LOG_FILES.clear()


def _session_headers(client: TestClient, origin: str = LOCAL_ORIGIN) -> dict[str, str]:
    response = client.get(f"{API_PREFIX}/session", headers={"Origin": origin})
    assert response.status_code == 200
    token = response.json()["csrf_token"]
    return {"Origin": origin, "X-CSRF-Token": token}


def _upload_pdf(
    client: TestClient,
    headers: dict[str, str],
    content: bytes = b"%PDF-1.7\nbody",
) -> str:
    response = client.post(
        f"{API_PREFIX}/uploads",
        headers=headers,
        files={"file": ("paper.pdf", content, "application/pdf")},
    )
    assert response.status_code == 200
    return response.json()["upload_id"]


def _job_payload(upload_id: str, **overrides: object) -> dict[str, object]:
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


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _multipart_body(content: bytes, *, boundary: str = "paper2code-boundary") -> bytes:
    return (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="paper.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")


def test_jobs_is_always_spa_and_api_jobs_is_always_json(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = secure_client

    spa = client.get("/jobs", headers={"Accept": "application/json"})
    api = client.get(f"{API_PREFIX}/jobs", headers={"Accept": "text/html"})

    assert spa.status_code == 200
    assert "text/html" in spa.headers["content-type"]
    assert '<div id="root"></div>' in spa.text
    assert spa.headers["cache-control"] == "no-cache"
    assert api.status_code == 200
    assert "application/json" in api.headers["content-type"]
    assert api.json() == {"jobs": []}
    assert api.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("accept", ["text/html", "*/*", "application/xml"])
def test_api_unknown_and_validation_errors_never_return_html(
    secure_client: tuple[TestClient, Path, Path],
    accept: str,
) -> None:
    client, _, _ = secure_client

    missing = client.get(f"{API_PREFIX}/does-not-exist", headers={"Accept": accept})
    invalid = client.get(f"{API_PREFIX}/jobs", params={"limit": 0}, headers={"Accept": accept})

    for response in (missing, invalid):
        assert "application/json" in response.headers["content-type"]
        assert "<html" not in response.text.lower()
        assert response.headers["cache-control"] == "no-store"
    assert missing.status_code == 404
    assert invalid.status_code == 422


def test_session_sets_strict_cookie_and_csrf_is_required(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = secure_client
    session = client.get(f"{API_PREFIX}/session", headers={"Origin": LOCAL_ORIGIN})

    assert session.status_code == 200
    assert session.json()["csrf_token"]
    cookie = session.headers["set-cookie"].lower()
    assert "samesite=strict" in cookie
    assert "httponly" in cookie

    rejected = client.post(
        f"{API_PREFIX}/settings",
        headers={"Origin": LOCAL_ORIGIN},
        json=_settings_payload(),
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "csrf_failed"


def test_malformed_session_cookie_is_rotated_without_server_error(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = secure_client
    client.cookies.set("paper2code_session", "invalid-cookie!")

    response = client.get(f"{API_PREFIX}/session")

    assert response.status_code == 200
    assert response.json()["csrf_token"]
    assert "invalid-cookie!" not in response.headers["set-cookie"]


@pytest.mark.parametrize("body_kind", ["json", "form", "multipart", "text"])
def test_evil_origin_is_rejected_before_create_job_for_all_content_types(
    secure_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    body_kind: str,
) -> None:
    client, _, _ = secure_client
    headers = _session_headers(client)
    headers["Origin"] = EVIL_ORIGIN
    calls: list[str] = []
    monkeypatch.setattr(main_module, "start_job", lambda **kwargs: calls.append("start"))

    request_kwargs: dict[str, object]
    if body_kind == "json":
        request_kwargs = {"json": {"upload_id": "abc"}}
    elif body_kind == "form":
        request_kwargs = {"data": {"upload_id": "abc"}}
    elif body_kind == "multipart":
        request_kwargs = {"files": {"file": ("paper.pdf", b"%PDF-", "application/pdf")}}
    else:
        headers["Content-Type"] = "text/plain"
        request_kwargs = {"content": "upload_id=abc"}

    response = client.post(f"{API_PREFIX}/jobs", headers=headers, **request_kwargs)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert calls == []


@pytest.mark.parametrize("body_kind", ["json", "form", "multipart", "text"])
def test_evil_origin_is_rejected_before_cancel_for_all_content_types(
    secure_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    body_kind: str,
) -> None:
    client, _, _ = secure_client
    headers = _session_headers(client)
    headers["Origin"] = EVIL_ORIGIN
    calls: list[str] = []
    monkeypatch.setattr(main_module, "cancel_job", lambda job_id: calls.append(job_id))

    request_kwargs: dict[str, object]
    if body_kind == "json":
        request_kwargs = {"json": {}}
    elif body_kind == "form":
        request_kwargs = {"data": {"ignored": "1"}}
    elif body_kind == "multipart":
        request_kwargs = {"files": {"ignored": (None, "1")}}
    else:
        headers["Content-Type"] = "text/plain"
        request_kwargs = {"content": "cancel"}

    response = client.post(
        f"{API_PREFIX}/jobs/job1/cancel",
        headers=headers,
        **request_kwargs,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert calls == []


def test_cross_site_fetch_metadata_is_rejected_before_mutation(
    secure_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = secure_client
    headers = _session_headers(client)
    headers["Sec-Fetch-Site"] = "cross-site"
    called = False

    def record_cancel(job_id: str):
        nonlocal called
        called = True
        return True, "unexpected"

    monkeypatch.setattr(main_module, "cancel_job", record_cancel)
    response = client.post(f"{API_PREFIX}/jobs/job1/cancel", headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cross_site_request"
    assert called is False


def test_sqlite_cancel_security_checks_precede_repository_and_legacy_cancel(
    secure_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = secure_client
    monkeypatch.setenv("JOB_RUNTIME", "sqlite")
    repository_calls: list[str] = []
    cancel_calls: list[str] = []

    def forbidden_repository() -> None:
        repository_calls.append("repository")
        raise AssertionError(
            "security middleware must reject before repository access"
        )

    monkeypatch.setattr(main_module, "_sqlite_repository", forbidden_repository)
    monkeypatch.setattr(
        main_module,
        "cancel_job",
        lambda job_id: cancel_calls.append(job_id),
    )
    authorized = _session_headers(client)

    evil_origin = client.post(
        f"{API_PREFIX}/jobs/job1/cancel",
        headers={**authorized, "Origin": EVIL_ORIGIN},
    )
    missing_csrf = client.post(
        f"{API_PREFIX}/jobs/job1/cancel",
        headers={"Origin": LOCAL_ORIGIN},
    )
    cross_site = client.post(
        f"{API_PREFIX}/jobs/job1/cancel",
        headers={**authorized, "Sec-Fetch-Site": "cross-site"},
    )

    assert evil_origin.status_code == 403
    assert evil_origin.json()["error"]["code"] == "origin_not_allowed"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "csrf_failed"
    assert cross_site.status_code == 403
    assert cross_site.json()["error"]["code"] == "cross_site_request"
    assert repository_calls == []
    assert cancel_calls == []


def test_untrusted_host_is_rejected_with_json() -> None:
    client = TestClient(
        main_module.app,
        base_url="http://evil.example",
        raise_server_exceptions=False,
    )

    response = client.get(f"{API_PREFIX}/jobs", headers={"Accept": "text/html"})

    assert response.status_code == 400
    assert "application/json" in response.headers["content-type"]
    assert response.json()["error"]["code"] == "invalid_host"


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]"])
def test_all_loopback_host_forms_are_allowed(host: str) -> None:
    client = TestClient(
        main_module.app,
        base_url="http://localhost",
        raise_server_exceptions=False,
    )

    response = client.get(
        f"{API_PREFIX}/health",
        headers={"Accept": "text/html", "Host": host},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_same_origin_referer_is_accepted_when_origin_is_absent(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = secure_client
    session = client.get(f"{API_PREFIX}/session")
    response = client.post(
        f"{API_PREFIX}/settings",
        headers={
            "Referer": f"{LOCAL_ORIGIN}/settings",
            "X-CSRF-Token": session.json()["csrf_token"],
        },
        json=_settings_payload(),
    )

    assert response.status_code == 200


def test_validation_errors_never_echo_raw_settings_inputs(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = secure_client
    headers = _session_headers(client)
    reproduce_secret = "REPRODUCE-" + "UNIQUE-CREDENTIAL"
    evaluation_secret = "EVALUATION-" + "UNIQUE-CREDENTIAL"
    payload = _settings_payload()
    payload["reproduce"]["api_key"] = [reproduce_secret]  # type: ignore[index]
    payload["evaluation"]["api_key"] = [evaluation_secret]  # type: ignore[index]

    response = client.post(f"{API_PREFIX}/settings", headers=headers, json=payload)

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    if reproduce_secret.encode() in response.content:
        pytest.fail("validation response leaked the reproduce credential", pytrace=False)
    if evaluation_secret.encode() in response.content:
        pytest.fail("validation response leaked the evaluation credential", pytrace=False)
    details = body["error"]["details"]
    assert not _contains_key(details, "input")
    assert details["errors"]
    for error in details["errors"]:
        assert set(error) == {"type", "loc", "msg"}
        assert error["type"]
        assert error["loc"]
        assert error["msg"]


def test_ordinary_validation_error_keeps_safe_location_fields(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = secure_client

    response = client.get(f"{API_PREFIX}/jobs", params={"limit": 0})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    errors = body["error"]["details"]["errors"]
    assert errors
    assert set(errors[0]) == {"type", "loc", "msg"}
    assert errors[0]["loc"] == ["query", "limit"]
    assert errors[0]["type"]
    assert errors[0]["msg"]


def test_raw_upload_limit_uses_received_bytes_before_endpoint(
    secure_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, uploads_dir = secure_client
    headers = _session_headers(client)
    body = _multipart_body(b"%PDF-" + b"x" * 64)
    monkeypatch.setattr(web_security, "MAX_UPLOAD_REQUEST_BYTES", len(body) - 1)
    observed_content_lengths: list[int | None] = []
    original_content_length = web_security.UploadBodyLimitMiddleware._content_length

    def record_content_length(scope) -> int | None:
        value = original_content_length(scope)
        observed_content_lengths.append(value)
        return value

    monkeypatch.setattr(
        web_security.UploadBodyLimitMiddleware,
        "_content_length",
        staticmethod(record_content_length),
    )
    headers.update(
        {
            "Content-Type": "multipart/form-data; boundary=paper2code-boundary",
            "Content-Length": "1",
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(main_module, "save_upload", lambda *args: calls.append("save"))

    response = client.post(f"{API_PREFIX}/uploads", headers=headers, content=body)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert response.headers["cache-control"] == "no-store"
    assert observed_content_lengths == [1]
    assert calls == []
    assert not [path for path in uploads_dir.rglob("*") if path.is_file()]
    assert not list(uploads_dir.rglob("*.tmp"))


def test_evil_origin_precedes_raw_limit_and_multipart_parser(
    secure_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, uploads_dir = secure_client
    headers = _session_headers(client)
    headers["Origin"] = EVIL_ORIGIN
    body = _multipart_body(b"%PDF-" + b"x" * 128)
    monkeypatch.setattr(web_security, "MAX_UPLOAD_REQUEST_BYTES", 32)
    headers["Content-Type"] = "multipart/form-data; boundary=paper2code-boundary"
    parser_calls: list[str] = []
    save_calls: list[str] = []

    class RecordingParser:
        def __init__(self, *args, **kwargs) -> None:
            parser_calls.append("parse")
            raise AssertionError("multipart parser must not run for an evil origin")

    monkeypatch.setattr("starlette.requests.MultiPartParser", RecordingParser)
    monkeypatch.setattr(main_module, "save_upload", lambda *args: save_calls.append("save"))

    response = client.post(f"{API_PREFIX}/uploads", headers=headers, content=body)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert parser_calls == []
    assert save_calls == []
    assert not [path for path in uploads_dir.rglob("*") if path.is_file()]


def test_upload_limit_exception_closes_starlette_spooled_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_files: list[tempfile.SpooledTemporaryFile] = []
    original_spooled_file = tempfile.SpooledTemporaryFile

    def recording_spooled_file(*args, **kwargs):
        file = original_spooled_file(*args, **kwargs)
        created_files.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", recording_spooled_file)
    boundary = "cleanup-boundary"
    first_chunk = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="paper.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "%PDF-partial"
    ).encode("ascii")

    async def stream():
        yield first_chunk
        raise web_security.UploadBodyTooLarge("request body exceeded the limit")

    parser = formparsers.MultiPartParser(
        Headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
        stream(),
    )

    with pytest.raises(web_security.UploadBodyTooLarge):
        asyncio.run(parser.parse())

    assert created_files
    assert all(file.closed for file in created_files)


def test_raw_limit_replaces_parser_error_with_413_after_closing_spool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_files: list[tempfile.SpooledTemporaryFile] = []
    original_spooled_file = tempfile.SpooledTemporaryFile

    def recording_spooled_file(*args, **kwargs):
        file = original_spooled_file(*args, **kwargs)
        created_files.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", recording_spooled_file)
    boundary = "stream-boundary"
    first_chunk = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="paper.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "%PDF-partial"
    ).encode("ascii")
    second_chunk = b"x" * 32 + f"\r\n--{boundary}--\r\n".encode("ascii")
    messages = iter(
        [
            {"type": "http.request", "body": first_chunk, "more_body": True},
            {"type": "http.request", "body": second_chunk, "more_body": False},
        ]
    )
    sent: list[dict] = []

    async def receive():
        return next(messages)

    async def send(message) -> None:
        sent.append(message)

    async def parse_multipart(scope, receive, send) -> None:
        request = Request(scope, receive)
        try:
            await request.form()
        except HTTPException:
            response = JSONResponse({"downstream": "parser_error"}, status_code=400)
            await response(scope, receive, send)

    middleware = web_security.UploadBodyLimitMiddleware(
        parse_multipart,
        max_body_bytes=len(first_chunk) + 1,
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"{API_PREFIX}/uploads",
        "raw_path": f"{API_PREFIX}/uploads".encode("ascii"),
        "query_string": b"",
        "headers": [
            (b"content-type", f"multipart/form-data; boundary={boundary}".encode("ascii")),
            (b"content-length", b"1"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    asyncio.run(middleware(scope, receive, send))

    start_messages = [message for message in sent if message["type"] == "http.response.start"]
    body_messages = [message for message in sent if message["type"] == "http.response.body"]
    assert [message["status"] for message in start_messages] == [413]
    assert len(body_messages) == 1
    assert b'"code":"file_too_large"' in body_messages[0]["body"]
    assert created_files
    assert all(file.closed for file in created_files)


def test_save_upload_small_exact_limit_and_one_byte_over(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    _, _, uploads_dir = secure_client
    exact_content = b"%PDF-exact"
    exact_id = "a" * 32
    too_large_id = "b" * 32

    # Scale the 100 MiB production boundary down without creating a large file.
    exact_stream = tempfile.SpooledTemporaryFile()
    exact_stream.write(exact_content)
    exact_stream.seek(0)
    exact_path = job_service.save_upload(
        exact_id,
        "paper.pdf",
        exact_stream,
        max_bytes=len(exact_content),
    )
    too_large_stream = tempfile.SpooledTemporaryFile()
    too_large_stream.write(exact_content + b"x")
    too_large_stream.seek(0)
    with pytest.raises(FileTooLargeError):
        job_service.save_upload(
            too_large_id,
            "paper.pdf",
            too_large_stream,
            max_bytes=len(exact_content),
        )
    exact_stream.close()
    too_large_stream.close()

    assert exact_path.read_bytes() == exact_content
    assert not (uploads_dir / too_large_id / "document.pdf").exists()
    assert not list(uploads_dir.rglob("*.tmp"))


def test_upload_disk_copy_does_not_block_health_on_single_event_loop(
    secure_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, _, _ = secure_client
    headers = _session_headers(client)
    session_id = client.cookies.get("paper2code_session")
    saved_path = tmp_path / "saved.pdf"
    saved_path.write_bytes(b"%PDF-saved")
    started = threading.Event()
    health_completed = threading.Event()
    release_upload = threading.Event()
    health_preceded_release = threading.Event()
    upload_sources: list[object] = []

    def slow_save_upload(upload_id: str, filename: str, source) -> Path:
        upload_sources.append(source)
        started.set()
        if not release_upload.wait(3):
            raise RuntimeError("test did not release the upload worker")
        return saved_path

    monkeypatch.setattr(main_module, "save_upload", slow_save_upload)

    def control_release() -> None:
        if not started.wait(3):
            release_upload.set()
            return
        if health_completed.wait(1):
            health_preceded_release.set()
        release_upload.set()

    controller = threading.Thread(target=control_release)
    controller.start()

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=main_module.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=LOCAL_ORIGIN,
            headers={**headers, "Cookie": f"paper2code_session={session_id}"},
        ) as async_client:
            upload_task = asyncio.create_task(
                async_client.post(
                    f"{API_PREFIX}/uploads",
                    files={"file": ("paper.pdf", b"%PDF-body", "application/pdf")},
                )
            )
            assert await asyncio.to_thread(started.wait, 2)
            health_response = await async_client.get(f"{API_PREFIX}/health")
            health_completed.set()
            upload_response = await asyncio.wait_for(upload_task, timeout=3)
            return health_response, upload_response

    try:
        health_response, upload_response = asyncio.run(exercise())
    finally:
        release_upload.set()
        controller.join(timeout=3)

    assert health_preceded_release.is_set()
    assert health_response.status_code == 200
    assert upload_response.status_code == 200
    assert upload_sources
    assert all(getattr(source, "closed", False) for source in upload_sources)


def test_upload_limit_uses_streamed_bytes_not_content_length_and_cleans_temp_files(
    secure_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, uploads_dir = secure_client
    monkeypatch.setattr(job_service, "MAX_PDF_UPLOAD_BYTES", 10)
    headers = _session_headers(client)
    headers["Content-Length"] = "1"

    response = client.post(
        f"{API_PREFIX}/uploads",
        headers=headers,
        files={"file": ("paper.pdf", b"%PDF-123456789", "application/pdf")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert not [path for path in uploads_dir.rglob("*") if path.is_file()]
    assert not list(uploads_dir.rglob("*.tmp"))


def test_production_same_origin_upload_and_explicit_vite_origin_work(
    secure_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = secure_client

    local_headers = _session_headers(client, LOCAL_ORIGIN)
    local_upload = _upload_pdf(client, local_headers)
    vite_headers = _session_headers(client, VITE_ORIGIN)
    vite_upload = _upload_pdf(client, vite_headers)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_module,
        "load_settings",
        lambda: WebSettings(**_settings_payload()),
    )

    def fake_start_job(**kwargs):
        calls.append(("start", kwargs["job_id"]))
        return {
            "job_id": kwargs["job_id"],
            "status": "queued",
            "run_dir": "runs/job",
            "status_path": "runs/job/run_status.json",
            "summary_path": "runs/job/run_summary.json",
        }

    def fake_cancel_job(job_id: str):
        calls.append(("cancel", job_id))
        return True, "canceled"

    monkeypatch.setattr(main_module, "start_job", fake_start_job)
    monkeypatch.setattr(main_module, "cancel_job", fake_cancel_job)
    created = client.post(
        f"{API_PREFIX}/jobs",
        headers=local_headers,
        json=_job_payload(local_upload),
    )
    canceled = client.post(
        f"{API_PREFIX}/jobs/job1/cancel",
        headers=vite_headers,
    )

    assert local_upload != vite_upload
    assert created.status_code == 200
    assert canceled.status_code == 200
    assert [call[0] for call in calls] == ["start", "cancel"]


def test_upload_uses_server_generated_name(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    client, _, uploads_dir = secure_client
    headers = _session_headers(client)

    response = client.post(
        f"{API_PREFIX}/uploads",
        headers=headers,
        files={"file": ("../../attacker.pdf", b"%PDF-1.7\nbody", "application/pdf")},
    )

    assert response.status_code == 200
    files = [path for path in uploads_dir.rglob("*") if path.is_file()]
    assert [path.name for path in files] == ["document.pdf"]
    assert "attacker" not in str(files[0])


def test_ref_based_returns_feature_not_supported_without_starting_job(
    secure_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = secure_client
    headers = _session_headers(client)
    upload_id = _upload_pdf(client, headers)
    monkeypatch.setattr(main_module, "load_settings", lambda: object())
    calls: list[str] = []
    monkeypatch.setattr(main_module, "start_job", lambda **kwargs: calls.append("start"))

    response = client.post(
        f"{API_PREFIX}/jobs",
        headers=headers,
        json=_job_payload(upload_id, eval_type="ref_based"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "feature_not_supported"
    assert calls == []


def test_settings_status_contains_only_boolean_state_and_never_keys(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = secure_client
    headers = _session_headers(client)
    saved = client.post(f"{API_PREFIX}/settings", headers=headers, json=_settings_payload())
    status = client.get(f"{API_PREFIX}/settings/status")

    assert saved.status_code == 200
    assert status.status_code == 200
    body = status.json()
    assert body == {
        "configured": True,
        "reproduce": {"has_api_key": True},
        "evaluation": {"has_api_key": True},
    }
    assert "secret" not in status.text
    assert all(
        isinstance(value, bool)
        for section in body.values()
        for value in (section.values() if isinstance(section, dict) else [section])
    )


def test_unconfigured_settings_status_still_contains_only_booleans(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = secure_client

    response = client.get(f"{API_PREFIX}/settings/status")

    assert response.json() == {
        "configured": False,
        "reproduce": {"has_api_key": False},
        "evaluation": {"has_api_key": False},
    }


def test_hashed_assets_use_immutable_cache(
    secure_client: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = secure_client
    assets_dir = Path(main_module.__file__).resolve().parents[1] / "web_ui" / "dist" / "assets"
    asset = next(path for path in assets_dir.iterdir() if path.is_file() and "-" in path.stem)

    response = client.get(f"/assets/{asset.name}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
