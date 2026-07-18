from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

from web_api import artifact_service, log_service, main as main_module
from web_api.errors import FileTooLargeError


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


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../../outside.py",
        r"..\outside.py",
        r"C:\outside.py",
        r"\\server\share\x.py",
        "a:b.py",
    ],
)
def test_repo_preview_rejects_unsafe_paths_without_touching_sentinel(
    artifacts_client: tuple[TestClient, Path],
    unsafe_path: str,
) -> None:
    client, runs_dir = artifacts_client
    _make_job(runs_dir)
    sentinel = runs_dir.parent / "outside.py"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = sentinel.stat()

    response = client.get("/jobs/job1/repo/file", params={"path": unsafe_path})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_repo_path"
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sentinel.stat().st_mtime_ns == before.st_mtime_ns


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

    report_dir = run_dir / "results"
    report_dir.mkdir()
    (report_dir / "report.txt").write_text("separate report root", encoding="utf-8")

    response = client.get("/jobs/job1/repo/download")

    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(response.content)) as zf:
        names = sorted(zf.namelist())
        assert names == ["main.py"]
        assert not any(".local/" in name for name in names)
        assert not any("web_settings.json" in name for name in names)
        assert not any(".downloads/" in name for name in names)
        assert not any("report.txt" in name for name in names)


def _create_symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"symlink creation is not permitted on this host: {exc}")


def test_repo_preview_and_zip_reject_symlink_and_never_export_sentinel(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    sentinel = runs_dir.parent / "sentinel.txt"
    sentinel.write_text("outside-secret", encoding="utf-8")
    link = repo_dir / "linked.txt"
    _create_symlink_or_skip(link, sentinel, directory=False)

    preview = client.get("/jobs/job1/repo/file", params={"path": "linked.txt"})
    download = client.get("/jobs/job1/repo/download")

    assert preview.status_code == 400
    assert preview.json()["error"]["code"] == "invalid_repo_path"
    assert download.status_code == 400
    assert b"outside-secret" not in download.content
    assert sentinel.read_text(encoding="utf-8") == "outside-secret"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction test")
def test_repo_tree_and_zip_reject_junction_parent(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    outside = runs_dir.parent / "outside-report"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("junction-secret", encoding="utf-8")
    junction = repo_dir / "linked-dir"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation failed: {result.stderr or result.stdout}")
    try:
        tree = client.get("/jobs/job1/repo/tree")
        download = client.get("/jobs/job1/repo/download")

        assert tree.status_code == 400
        assert download.status_code == 400
        assert b"junction-secret" not in download.content
        assert sentinel.read_text(encoding="utf-8") == "junction-secret"
    finally:
        junction.rmdir()


def test_repo_preview_and_zip_reject_hardlink_and_never_export_sentinel(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    sentinel = runs_dir.parent / "sentinel.txt"
    sentinel.write_text("hardlink-secret", encoding="utf-8")
    hardlink = repo_dir / "hardlink.txt"
    try:
        os.link(sentinel, hardlink)
    except OSError as exc:
        pytest.skip(f"hardlink creation is not supported on this host: {exc}")

    preview = client.get("/jobs/job1/repo/file", params={"path": "hardlink.txt"})
    download = client.get("/jobs/job1/repo/download")

    assert preview.status_code == 400
    assert download.status_code == 400
    assert b"hardlink-secret" not in download.content
    assert sentinel.read_text(encoding="utf-8") == "hardlink-secret"


def test_repo_zip_enforces_file_count_single_file_and_total_limits(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    _, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    (repo_dir / "a.txt").write_bytes(b"aaaa")
    (repo_dir / "b.txt").write_bytes(b"bbbb")

    with pytest.raises(FileTooLargeError, match="file count"):
        artifact_service.make_repo_zip("job1", max_files=1)
    with pytest.raises(FileTooLargeError, match="single-file"):
        artifact_service.make_repo_zip("job1", max_file_bytes=3)
    with pytest.raises(FileTooLargeError, match="total"):
        artifact_service.make_repo_zip("job1", max_total_bytes=7)


def test_repo_zip_rechecks_actual_total_after_files_grow_post_collection(
    artifacts_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    for name in ("a.txt", "b.txt"):
        (repo_dir / name).write_bytes(b"aaaa")

    real_collect = artifact_service._collect_safe_files

    def grow_after_collection(root: Path, **limits):
        collected = real_collect(root, **limits)
        for path, _, _ in collected:
            path.write_bytes(b"x" * 9)
        # Refresh only the per-file identity snapshot so this test isolates
        # make_repo_zip's actual-byte total from the collection precheck.
        return [
            (path, relative_path, path.lstat())
            for path, relative_path, _ in collected
        ]

    monkeypatch.setattr(
        artifact_service,
        "_collect_safe_files",
        grow_after_collection,
    )

    with pytest.raises(FileTooLargeError, match="actual total"):
        artifact_service.make_repo_zip(
            "job1",
            max_files=2,
            max_file_bytes=10,
            max_total_bytes=10,
        )

    downloads_dir = runs_dir / "job1" / ".downloads"
    assert not list(downloads_dir.glob("*.zip"))
    assert not list(downloads_dir.glob("*.tmp"))


def test_repo_zip_rejects_growth_against_collected_file_snapshot(
    artifacts_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    for name in ("a.txt", "b.txt"):
        (repo_dir / name).write_bytes(b"aaaa")

    real_collect = artifact_service._collect_safe_files

    def grow_after_collection(root: Path, **limits):
        collected = real_collect(root, **limits)
        for path, _, _ in collected:
            path.write_bytes(b"x" * 9)
        return collected

    monkeypatch.setattr(
        artifact_service,
        "_collect_safe_files",
        grow_after_collection,
    )

    with pytest.raises(FileTooLargeError, match="grew after collection"):
        artifact_service.make_repo_zip(
            "job1",
            max_files=2,
            max_file_bytes=10,
            max_total_bytes=10,
        )

    downloads_dir = runs_dir / "job1" / ".downloads"
    assert not list(downloads_dir.glob("*.zip"))
    assert not list(downloads_dir.glob("*.tmp"))


def test_repo_zip_actual_total_limit_allows_normal_small_files(
    artifacts_client: tuple[TestClient, Path],
) -> None:
    _, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    (repo_dir / "a.txt").write_bytes(b"aaaa")
    (repo_dir / "b.txt").write_bytes(b"bbbb")

    zip_path = artifact_service.make_repo_zip(
        "job1",
        max_files=2,
        max_file_bytes=10,
        max_total_bytes=10,
    )

    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read("a.txt") == b"aaaa"
        assert zf.read("b.txt") == b"bbbb"


def test_same_open_file_rejects_size_or_mtime_changes(tmp_path: Path) -> None:
    size_path = tmp_path / "size.txt"
    size_path.write_bytes(b"aaaa")
    size_before = size_path.stat()
    size_path.write_bytes(b"x" * 9)
    assert not artifact_service._same_open_file(size_before, size_path.stat())

    mtime_path = tmp_path / "mtime.txt"
    mtime_path.write_bytes(b"same")
    mtime_before = mtime_path.stat()
    os.utime(
        mtime_path,
        ns=(mtime_before.st_atime_ns, mtime_before.st_mtime_ns + 2_000_000_000),
    )
    mtime_after = mtime_path.stat()
    assert mtime_after.st_mtime_ns != mtime_before.st_mtime_ns
    assert not artifact_service._same_open_file(mtime_before, mtime_after)


def test_repo_preview_opens_then_revalidates_with_fstat(
    artifacts_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runs_dir = artifacts_client
    _, repo_dir, _ = _make_job(runs_dir)
    (repo_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    real_fstat = artifact_service.os.fstat
    calls: list[int] = []

    def recording_fstat(fd: int):
        calls.append(fd)
        return real_fstat(fd)

    monkeypatch.setattr(artifact_service.os, "fstat", recording_fstat)

    response = client.get("/jobs/job1/repo/file", params={"path": "main.py"})

    assert response.status_code == 200
    assert calls


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
