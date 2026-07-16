from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient

from web_api import artifact_service, log_service, main as main_module


@pytest.fixture()
def artifacts_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(artifact_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(log_service, "RUNS_DIR", runs_dir)
    return TestClient(main_module.app), runs_dir


def _make_job(runs_dir: Path, job_id: str = "job1") -> tuple[Path, Path, Path]:
    run_dir = runs_dir / job_id
    repo_dir = run_dir / "repo"
    logs_dir = run_dir / "logs"
    repo_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return run_dir, repo_dir, logs_dir


def test_repo_file_rejects_large_text_file(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    big_file = repo_dir / "big.txt"
    big_file.write_bytes(b"a" * (artifact_service.MAX_TEXT_FILE_BYTES + 1))

    response = client.get("/jobs/job1/repo/file", params={"path": "big.txt"})

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


def test_repo_file_rejects_binary_file_even_with_text_extension(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    (repo_dir / "binary.txt").write_bytes(b"text\x00binary")

    response = client.get("/jobs/job1/repo/file", params={"path": "binary.txt"})

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "binary_file_not_supported"


def test_repo_file_rejects_disallowed_extension(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    (repo_dir / "data.bin").write_bytes(b"plain text but disallowed")

    response = client.get("/jobs/job1/repo/file", params={"path": "data.bin"})

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_file_type"


def test_logs_missing_job_returns_404(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, _ = artifacts_client

    response = client.get("/jobs/missing_job/logs")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_existing_job_without_logs_returns_empty_logs(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    (runs_dir / "job_without_logs").mkdir()

    response = client.get("/jobs/job_without_logs/logs")

    assert response.status_code == 200
    assert response.json()["logs"] == []


def test_large_log_tail_returns_only_requested_lines(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    _, _, logs_dir = _make_job(runs_dir)
    log_path = logs_dir / "run.log"
    log_path.write_text(
        "".join(f"line {i}\n" for i in range(20_000)),
        encoding="utf-8",
    )

    response = client.get(
        "/jobs/job1/logs",
        params={"file": "run.log", "tail_lines": 3},
    )

    assert response.status_code == 200
    assert response.json()["content"].splitlines() == [
        "line 19997",
        "line 19998",
        "line 19999",
    ]


def test_repeated_and_concurrent_repo_downloads_are_unique_and_complete(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    (repo_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (repo_dir / "README.md").write_text("# ok\n", encoding="utf-8")

    first_response = client.get("/jobs/job1/repo/download")
    second_response = client.get("/jobs/job1/repo/download")
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    for response in [first_response, second_response]:
        assert zipfile.is_zipfile(BytesIO(response.content))
        with zipfile.ZipFile(BytesIO(response.content)) as zf:
            assert sorted(zf.namelist()) == ["README.md", "main.py"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        zip_paths = list(executor.map(lambda _: artifact_service.make_repo_zip("job1"), range(8)))

    assert len(set(zip_paths)) == 8
    for zip_path in zip_paths:
        assert zip_path.exists()
        assert zip_path.parent.name == ".downloads"
        assert zipfile.is_zipfile(zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            assert sorted(zf.namelist()) == ["README.md", "main.py"]
    assert not list((runs_dir / "job1" / ".downloads").glob("*.tmp"))


def test_repo_download_excludes_local_runtime_state_and_sibling_runtime_dirs(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    run_dir, repo_dir, _ = _make_job(runs_dir)
    (repo_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")

    local_root = runs_dir.parent / ".local"
    local_root.mkdir()
    (local_root / "web_settings.json").write_text(
        '{"api_key": "should-not-export"}',
        encoding="utf-8",
    )

    sibling_local_dir = run_dir / ".local"
    sibling_local_dir.mkdir()
    (sibling_local_dir / "secret.txt").write_text("hidden", encoding="utf-8")

    sibling_downloads_dir = run_dir / ".downloads"
    sibling_downloads_dir.mkdir(exist_ok=True)
    (sibling_downloads_dir / "old.zip").write_text("not a real zip", encoding="utf-8")

    response = client.get("/jobs/job1/repo/download")

    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(response.content)) as zf:
        names = sorted(zf.namelist())
        assert names == ["main.py"]
        assert not any(".local/" in name for name in names)
        assert not any("web_settings.json" in name for name in names)
        assert not any(".downloads/" in name for name in names)


def test_list_jobs_best_effort_with_many_bad_run_dirs(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    for index in range(250):
        run_dir = runs_dir / f"job_{index:03d}"
        run_dir.mkdir()
        (run_dir / "run_status.json").write_text(
            '{"status": "running",',
            encoding="utf-8",
        )
    good_run = runs_dir / "good_job"
    good_run.mkdir()
    (good_run / "run_status.json").write_text(
        '{"job_id": "good_job", "status": "completed"}',
        encoding="utf-8",
    )

    response = client.get("/jobs", params={"limit": 200})

    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 200
    assert any(job["job_id"] == "good_job" for job in body["jobs"])
