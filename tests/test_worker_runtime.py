import os
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_api import job_service, main as main_module
from web_api.database import connect_database
from web_api.job_repository import JobRepository
from web_api.process_identity import get_process_create_time
from web_api.schemas import WebSettings
from web_api.worker import LaunchSpec, PipelineWorker


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"


def _request(upload_id: str = "0" * 32) -> dict[str, object]:
    return {
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


def _create(repository: JobRepository, job_id: str, upload_id: str = "0" * 32):
    return repository.create_job(
        job_id=job_id,
        request=_request(upload_id),
        paper_name="paper",
    )[0]


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _settings() -> WebSettings:
    return WebSettings(
        reproduce={
            "provider": "openai",
            "model": "fake-model",
            "api_key": "fake-reproduce-key",
        },
        evaluation={
            "provider": "openai",
            "model": "fake-model",
            "api_key": "fake-evaluation-key",
            "fallback_models": [],
        },
    )


def _fake_pipeline_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_pipeline.py"
    script.write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "job_id, started_dir, release_dir, child_pid_dir = sys.argv[1:]\n"
        "Path(started_dir).mkdir(parents=True, exist_ok=True)\n"
        "Path(release_dir).mkdir(parents=True, exist_ok=True)\n"
        "Path(child_pid_dir).mkdir(parents=True, exist_ok=True)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "Path(child_pid_dir, job_id).write_text(str(child.pid), encoding='utf-8')\n"
        "Path(started_dir, job_id).write_text(str(os.getpid()), encoding='utf-8')\n"
        "while not Path(release_dir, job_id).exists():\n"
        "    time.sleep(0.05)\n"
        "child.terminate()\n"
        "child.wait(timeout=5)\n",
        encoding="utf-8",
    )
    return script


def _fake_builder(tmp_path: Path):
    script = _fake_pipeline_script(tmp_path)
    started_dir = tmp_path / "started"
    release_dir = tmp_path / "release"
    child_pid_dir = tmp_path / "children"

    def build(job: dict[str, object]) -> LaunchSpec:
        job_id = str(job["job_id"])
        return LaunchSpec(
            command=[
                sys.executable,
                str(script),
                job_id,
                str(started_dir),
                str(release_dir),
                str(child_pid_dir),
            ],
            environment=dict(os.environ),
            command_summary=f"python fake_pipeline.py --job-id {job_id}",
        )

    return build, started_dir, release_dir, child_pid_dir


def _stop_worker_jobs(worker: PipelineWorker, repository: JobRepository) -> None:
    for job in repository.list_jobs(limit=200):
        if job["execution_status"] in {"queued", "running"}:
            repository.request_cancel(str(job["job_id"]))
    _wait_until(
        lambda: (
            worker.run_once()
            or all(
                job["execution_status"] not in {"queued", "running"}
                for job in repository.list_jobs(limit=200)
            )
        )
        and all(
            job["execution_status"] not in {"queued", "running"}
            for job in repository.list_jobs(limit=200)
        ),
        timeout=5,
    )
    worker.close()


def test_worker_entrypoint_help_is_available() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "web_api.worker", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "max-concurrency" in result.stdout


def test_single_worker_claims_fifo_and_never_runs_two_jobs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(job_service, "RUNS_DIR", tmp_path / "runs")
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "job_first")
    _create(repository, "job_second")
    builder, started_dir, release_dir, _ = _fake_builder(tmp_path)
    worker = PipelineWorker(
        repository=repository,
        worker_id="worker-one",
        command_builder=builder,
        poll_interval=0.01,
        cancel_grace_seconds=0.2,
    )

    try:
        assert worker.max_concurrency == 1
        assert worker.run_once() is True
        assert _wait_until(lambda: (started_dir / "job_first").exists())
        worker.run_once()
        assert repository.get_job("job_first")["execution_status"] == "running"
        assert repository.get_job("job_second")["execution_status"] == "queued"
        assert not (started_dir / "job_second").exists()

        process = repository.get_process("job_first")
        assert process["worker_id"] == "worker-one"
        assert process["launch_token"]
        assert process["pid"] > 0
        assert process["process_create_time"]
        assert process["command_summary"] == (
            "python fake_pipeline.py --job-id job_first"
        )
        first_heartbeat = process["heartbeat_at"]
        time.sleep(0.02)
        worker.run_once()
        assert repository.get_process("job_first")["heartbeat_at"] > first_heartbeat

        release_dir.mkdir(parents=True, exist_ok=True)
        (release_dir / "job_first").touch()
        assert _wait_until(
            lambda: (
                worker.run_once()
                and repository.get_job("job_first")["execution_status"]
                == "completed"
            ),
        )
        assert _wait_until(
            lambda: (
                worker.run_once()
                and repository.get_job("job_second")["execution_status"] == "running"
            ),
        )
        assert _wait_until(lambda: (started_dir / "job_second").exists())
    finally:
        _stop_worker_jobs(worker, repository)


def test_only_global_lease_holder_can_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(job_service, "RUNS_DIR", tmp_path / "runs")
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "leased_job")
    builder, started_dir, _, _ = _fake_builder(tmp_path)
    owner = PipelineWorker(
        repository=repository,
        worker_id="lease-owner",
        command_builder=builder,
        cancel_grace_seconds=0.2,
    )
    contender = PipelineWorker(
        repository=repository,
        worker_id="lease-contender",
        command_builder=builder,
        cancel_grace_seconds=0.2,
    )

    try:
        assert owner.run_once() is True
        assert contender.run_once() is False
        assert _wait_until(lambda: (started_dir / "leased_job").exists())
        assert repository.get_process("leased_job")["worker_id"] == "lease-owner"
    finally:
        contender.close()
        _stop_worker_jobs(owner, repository)


def test_queued_cancel_is_completed_without_launch(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "queued_cancel")
    first = repository.request_cancel("queued_cancel")
    repeated = repository.request_cancel("queued_cancel")
    launch_calls: list[str] = []

    def forbidden_launch(job: dict[str, object]) -> LaunchSpec:
        launch_calls.append(str(job["job_id"]))
        raise AssertionError("queued canceled jobs must not launch")

    worker = PipelineWorker(
        repository=repository,
        worker_id="cancel-worker",
        command_builder=forbidden_launch,
    )
    try:
        assert first == repeated
        assert worker.run_once() is True
        assert repository.get_job("queued_cancel")["execution_status"] == "canceled"
        assert repository.get_cancel_command("queued_cancel")["status"] == "completed"
        assert launch_calls == []
    finally:
        worker.close()


def test_running_cancel_terminates_complete_process_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(job_service, "RUNS_DIR", tmp_path / "runs")
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "tree_cancel")
    builder, started_dir, _, child_pid_dir = _fake_builder(tmp_path)
    worker = PipelineWorker(
        repository=repository,
        worker_id="tree-worker",
        command_builder=builder,
        cancel_grace_seconds=0.2,
    )

    try:
        worker.run_once()
        assert _wait_until(lambda: (started_dir / "tree_cancel").exists())
        child_pid_path = child_pid_dir / "tree_cancel"
        assert _wait_until(child_pid_path.exists)
        process = repository.get_process("tree_cancel")
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

        repository.request_cancel("tree_cancel")
        assert _wait_until(
            lambda: (
                worker.run_once()
                and repository.get_job("tree_cancel")["execution_status"]
                == "canceled"
            )
        )
        assert not job_service.is_pid_alive(process["pid"])
        assert not job_service.is_pid_alive(child_pid)
        assert repository.get_cancel_command("tree_cancel")["status"] == "completed"
    finally:
        _stop_worker_jobs(worker, repository)


def test_process_identity_mismatch_never_kills_unrelated_process(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "identity_attack")
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **job_service.pipeline_popen_kwargs(),
    )
    worker = PipelineWorker(
        repository=repository,
        worker_id="identity-worker",
        command_builder=lambda job: (_ for _ in ()).throw(
            AssertionError("reconciliation must not launch a running job")
        ),
        cancel_grace_seconds=0.2,
    )

    try:
        assert repository.acquire_worker_lease("old-worker") is True
        claimed = repository.claim_next_queued_job(
            worker_id="old-worker",
            launch_token="old-launch-token",
        )
        assert claimed is not None
        assert repository.release_worker_lease("old-worker") is True
        repository.record_process_started(
            "identity_attack",
            launch_token="old-launch-token",
            pid=unrelated.pid,
            process_create_time="definitely-not-the-real-create-time",
            process_group_id=unrelated.pid,
            command_summary="python fake_pipeline.py --job-id identity_attack",
        )
        repository.request_cancel("identity_attack")

        assert worker.run_once() is True
        assert unrelated.poll() is None
        failed = repository.get_job("identity_attack")
        assert failed["execution_status"] == "failed"
        assert failed["failure_code"] == "process_identity_mismatch"
        assert repository.get_cancel_command("identity_attack")["status"] == "failed"
        with closing(connect_database(repository.database_path)) as connection:
            events = [
                row[0]
                for row in connection.execute(
                    "SELECT event_type FROM job_events WHERE job_id = ? ORDER BY id",
                    ("identity_attack",),
                )
            ]
        assert "job.process_identity_mismatch" in events
    finally:
        if unrelated.poll() is None:
            job_service.terminate_process_tree(unrelated.pid, unrelated, timeout=3)
        worker.close()


def test_startup_reconciliation_monitors_matching_process_and_leaves_queue(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "reconcile_running")
    _create(repository, "reconcile_queued")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **job_service.pipeline_popen_kwargs(),
    )
    create_time = get_process_create_time(process.pid, process)
    assert create_time is not None
    assert repository.acquire_worker_lease("old-worker") is True
    claimed = repository.claim_next_queued_job(
        worker_id="old-worker",
        launch_token="reconcile-launch-token",
    )
    assert claimed is not None
    repository.record_process_started(
        "reconcile_running",
        launch_token="reconcile-launch-token",
        pid=process.pid,
        process_create_time=create_time,
        process_group_id=job_service.process_group_id(process),
        command_summary="python fake_pipeline.py --job-id reconcile_running",
    )
    assert repository.release_worker_lease("old-worker") is True
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=lambda job: (_ for _ in ()).throw(
            AssertionError("a live reconciled process must occupy the only slot")
        ),
    )

    try:
        assert worker.run_once() is True
        assert repository.get_job("reconcile_running")["execution_status"] == "running"
        assert repository.get_job("reconcile_queued")["execution_status"] == "queued"
        assert repository.get_process("reconcile_running")["heartbeat_at"]
    finally:
        worker.close()
        if process.poll() is None:
            job_service.terminate_process_tree(process.pid, process, timeout=3)


def test_startup_reconciliation_marks_dead_process_as_explainable_failure(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "reconcile_dead")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **job_service.pipeline_popen_kwargs(),
    )
    create_time = get_process_create_time(process.pid, process)
    assert create_time is not None
    assert repository.acquire_worker_lease("old-worker") is True
    repository.claim_next_queued_job(
        worker_id="old-worker",
        launch_token="dead-launch-token",
    )
    repository.record_process_started(
        "reconcile_dead",
        launch_token="dead-launch-token",
        pid=process.pid,
        process_create_time=create_time,
        process_group_id=job_service.process_group_id(process),
        command_summary="python fake_pipeline.py --job-id reconcile_dead",
    )
    assert repository.release_worker_lease("old-worker") is True
    assert job_service.terminate_process_tree(process.pid, process, timeout=3)[0]
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=lambda job: (_ for _ in ()).throw(
            AssertionError("dead running jobs must not be relaunched")
        ),
    )

    try:
        assert worker.run_once() is True
        failed = repository.get_job("reconcile_dead")
        assert failed["execution_status"] == "failed"
        assert failed["failure_code"] == "process_exited_without_checkpoint"
    finally:
        worker.close()


def test_api_restart_does_not_affect_running_fake_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    uploads_dir = tmp_path / "uploads"
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("JOB_RUNTIME", "sqlite")
    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(db_path))
    monkeypatch.setattr(job_service, "UPLOADS_DIR", uploads_dir)
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(main_module, "load_settings", _settings)
    builder, started_dir, release_dir, _ = _fake_builder(tmp_path)
    repository = JobRepository(db_path)
    worker = PipelineWorker(
        repository=repository,
        worker_id="api-restart-worker",
        command_builder=builder,
        cancel_grace_seconds=0.2,
    )

    try:
        with TestClient(main_module.app, base_url=LOCAL_ORIGIN) as first_client:
            session = first_client.get(
                f"{API_PREFIX}/session", headers={"Origin": LOCAL_ORIGIN}
            )
            headers = {
                "Origin": LOCAL_ORIGIN,
                "X-CSRF-Token": session.json()["csrf_token"],
            }
            upload = first_client.post(
                f"{API_PREFIX}/uploads",
                headers=headers,
                files={"file": ("paper.pdf", b"%PDF-test", "application/pdf")},
            )
            created = first_client.post(
                f"{API_PREFIX}/jobs",
                headers=headers,
                json=_request(upload.json()["upload_id"]),
            )
            job_id = created.json()["job_id"]
            worker.run_once()
            assert _wait_until(lambda: (started_dir / job_id).exists())
            assert first_client.get(f"{API_PREFIX}/jobs/{job_id}").json()[
                "execution_status"
            ] == "running"

        with TestClient(main_module.app, base_url=LOCAL_ORIGIN) as restarted_client:
            running = restarted_client.get(f"{API_PREFIX}/jobs/{job_id}")
            assert running.status_code == 200
            assert running.json()["execution_status"] == "running"
            assert get_process_create_time(
                repository.get_process(job_id)["pid"]
            ) == repository.get_process(job_id)["process_create_time"]

            release_dir.mkdir(parents=True, exist_ok=True)
            (release_dir / job_id).touch()
            assert _wait_until(
                lambda: (
                    worker.run_once()
                    and repository.get_job(job_id)["execution_status"] == "completed"
                )
            )
            completed = restarted_client.get(f"{API_PREFIX}/jobs/{job_id}")
            assert completed.json()["execution_status"] == "completed"
    finally:
        _stop_worker_jobs(worker, repository)
