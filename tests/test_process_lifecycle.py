import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from web_api import job_service
from web_api.errors import JobNotCancelableError


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _spawn_parent_with_child(tmp_path: Path) -> tuple[subprocess.Popen, int]:
    child_pid_path = tmp_path / f"child_{time.monotonic_ns()}.pid"
    parent_script = tmp_path / f"parent_{time.monotonic_ns()}.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(parent_script), str(child_pid_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **job_service.pipeline_popen_kwargs(),
    )
    assert _wait_until(child_pid_path.exists)
    return proc, int(child_pid_path.read_text(encoding="utf-8"))


def _write_status(runs_dir: Path, job_id: str, **status: object) -> Path:
    run_dir = runs_dir / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    data = {"job_id": job_id, **status}
    (run_dir / "run_status.json").write_text(
        json.dumps(data),
        encoding="utf-8",
    )
    return run_dir


def _cleanup_process(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        job_service.terminate_process_tree(proc.pid, proc, timeout=3.0)


@pytest.fixture(autouse=True)
def _clear_active_processes():
    job_service.ACTIVE_PROCESSES.clear()
    job_service.ACTIVE_LOG_FILES.clear()
    yield
    job_service.ACTIVE_PROCESSES.clear()
    job_service.ACTIVE_LOG_FILES.clear()


def test_cancel_job_terminates_child_process_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    proc = None
    child_pid = None
    try:
        proc, child_pid = _spawn_parent_with_child(tmp_path)
        _write_status(
            runs_dir,
            "tree_job",
            status="running",
            process_pid=proc.pid,
            pid=proc.pid,
            process_group_id=job_service.process_group_id(proc),
            process_state="active",
            cancelable=True,
        )
        job_service.ACTIVE_PROCESSES["tree_job"] = proc

        canceled, message = job_service.cancel_job("tree_job")

        assert canceled is True
        assert "terminated" in message or "killed" in message
        assert _wait_until(lambda: proc.poll() is not None)
        assert _wait_until(lambda: not job_service.is_pid_alive(child_pid))
        status = job_service.read_json_file(runs_dir / "tree_job" / "run_status.json")
        assert status["status"] == "canceled"
        assert status["process_state"] == "finished"
        assert status["process_active"] is False
        assert status["cancelable"] is False
    finally:
        if child_pid and job_service.is_pid_alive(child_pid):
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
        _cleanup_process(proc)


def test_get_job_status_distinguishes_process_lifecycle_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    active_proc = None
    detached_proc = None
    try:
        active_proc, _ = _spawn_parent_with_child(tmp_path)
        _write_status(
            runs_dir,
            "active_job",
            status="running",
            process_pid=active_proc.pid,
            pid=active_proc.pid,
        )
        job_service.ACTIVE_PROCESSES["active_job"] = active_proc

        detached_proc, _ = _spawn_parent_with_child(tmp_path)
        _write_status(
            runs_dir,
            "detached_job",
            status="running",
            process_pid=detached_proc.pid,
            pid=detached_proc.pid,
        )

        missing_pid = 2_147_483_647
        _write_status(
            runs_dir,
            "orphaned_job",
            status="running",
            process_pid=missing_pid,
            pid=missing_pid,
        )
        _write_status(
            runs_dir,
            "finished_job",
            status="completed",
            process_pid=missing_pid,
            pid=missing_pid,
        )

        active = job_service.get_job_status("active_job")
        detached = job_service.get_job_status("detached_job")
        orphaned = job_service.get_job_status("orphaned_job")
        finished = job_service.get_job_status("finished_job")

        assert active["process_state"] == "active"
        assert active["process_active"] is True
        assert active["cancelable"] is True

        assert detached["process_state"] == "detached"
        assert detached["process_active"] is False
        assert detached["cancelable"] is False
        with pytest.raises(JobNotCancelableError) as exc_info:
            job_service.cancel_job("detached_job")
        assert exc_info.value.reason == "detached_after_restart"

        assert orphaned["process_state"] == "orphaned"
        assert orphaned["process_active"] is False
        assert orphaned["cancelable"] is False

        assert finished["process_state"] == "finished"
        assert finished["process_active"] is False
        assert finished["cancelable"] is False
    finally:
        _cleanup_process(active_proc)
        _cleanup_process(detached_proc)
