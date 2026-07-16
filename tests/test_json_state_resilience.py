import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_api import artifact_service, job_service, main as main_module


@pytest.fixture()
def state_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(artifact_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    return TestClient(main_module.app, raise_server_exceptions=False), runs_dir


def _make_run(runs_dir: Path, job_id: str) -> Path:
    run_dir = runs_dir / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _load_codes_utils():
    utils_path = Path(__file__).resolve().parents[1] / "codes" / "utils.py"
    spec = importlib.util.spec_from_file_location("codes_utils_for_json_test", utils_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_jobs_list_tolerates_corrupt_and_empty_state_json(
    state_client: tuple[TestClient, Path],
) -> None:
    client, runs_dir = state_client
    good_run = _make_run(runs_dir, "good_job")
    (good_run / "run_status.json").write_text(
        json.dumps({"job_id": "good_job", "status": "completed"}),
        encoding="utf-8",
    )
    corrupt_run = _make_run(runs_dir, "corrupt_job")
    (corrupt_run / "run_status.json").write_text(
        '{"status": "running",',
        encoding="utf-8",
    )
    empty_run = _make_run(runs_dir, "empty_job")
    (empty_run / "run_summary.json").write_text("", encoding="utf-8")

    response = client.get("/jobs")

    assert response.status_code == 200
    jobs_by_id = {job["job_id"]: job for job in response.json()["jobs"]}
    assert jobs_by_id["good_job"]["status"] == "completed"
    assert jobs_by_id["corrupt_job"]["status"] == "unknown"
    assert jobs_by_id["empty_job"]["status"] == "unknown"
    assert "unreadable" in jobs_by_id["corrupt_job"]["message"]


@pytest.mark.parametrize(
    "content",
    [
        '{"status": "running",',
        "",
    ],
)
def test_job_status_tolerates_corrupt_or_half_written_status_json(
    state_client: tuple[TestClient, Path],
    content: str,
) -> None:
    client, runs_dir = state_client
    run_dir = _make_run(runs_dir, "bad_status")
    (run_dir / "run_status.json").write_text(content, encoding="utf-8")

    response = client.get("/jobs/bad_status")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "bad_status"
    assert body["status"] == "unknown"
    assert body["process_active"] is False
    assert body["stage"] == "status_unavailable"


@pytest.mark.parametrize(
    "content",
    [
        '{"status": "completed",',
        "",
    ],
)
def test_job_summary_tolerates_corrupt_or_half_written_summary_json(
    state_client: tuple[TestClient, Path],
    content: str,
) -> None:
    client, runs_dir = state_client
    run_dir = _make_run(runs_dir, "bad_summary")
    (run_dir / "run_summary.json").write_text(content, encoding="utf-8")

    response = client.get("/jobs/bad_summary/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "bad_summary"
    assert body["status"] == "unknown"
    assert "unreadable" in body["message"]


def test_web_status_json_write_does_not_replace_existing_file_on_failure(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "run_status.json"
    job_service.write_json_file(status_path, {"status": "old"})

    with pytest.raises(TypeError):
        job_service.write_json_file(status_path, {"bad": object()})

    assert json.loads(status_path.read_text(encoding="utf-8")) == {"status": "old"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_codes_utils_loads_bad_state_json_as_default_and_writes_atomically(
    tmp_path: Path,
) -> None:
    utils = _load_codes_utils()
    state_path = tmp_path / "repo_status.json"
    state_path.write_text('{"status": "running",', encoding="utf-8")

    assert utils.load_json_file(str(state_path), default={"status": "unknown"}) == {
        "status": "unknown"
    }

    utils.save_json_file(str(state_path), {"status": "old"})
    with pytest.raises(TypeError):
        utils.save_json_file(str(state_path), {"bad": object()})

    assert json.loads(state_path.read_text(encoding="utf-8")) == {"status": "old"}
    assert list(tmp_path.glob("*.tmp")) == []
