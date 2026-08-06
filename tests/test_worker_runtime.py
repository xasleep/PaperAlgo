import io
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_api import job_service, main as main_module, worker as worker_module
from web_api.database import connect_database
from web_api.errors import OptimisticLockConflictError
from web_api.job_repository import JobRepository
from web_api.process_identity import TerminationResult, get_process_create_time
from web_api.schemas import WebSettings
from web_api.worker import LaunchSpec, ManagedProcess, PipelineWorker


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
            "model": "gpt-4.1-mini",
            "api_key": "fake-reproduce-key",
            "base_url": "https://reproduce.invalid/v1",
        },
        evaluation={
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "api_key": "fake-evaluation-key",
            "base_url": "https://evaluation.invalid/v1",
            "fallback_models": [],
        },
    )


def _settings_for(provider: str, model: str, *, key_suffix: str) -> WebSettings:
    return WebSettings(
        reproduce={
            "provider": provider,
            "model": model,
            "api_key": f"fake-reproduce-{key_suffix}",
            "base_url": f"https://reproduce-{key_suffix}.invalid/v1",
        },
        evaluation={
            "provider": provider,
            "model": model,
            "api_key": f"fake-evaluation-{key_suffix}",
            "base_url": f"https://evaluation-{key_suffix}.invalid/v1",
            "fallback_models": [],
        },
    )


def _selection_snapshot(settings: WebSettings | None = None) -> dict[str, object]:
    builder = getattr(job_service, "build_provider_selection_snapshot")
    return builder(settings or _settings())


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


def _registered_managed_job(
    repository: JobRepository,
    worker: PipelineWorker,
    job_id: str,
    process: object | None,
    *,
    pid: int = 4321,
) -> ManagedProcess:
    _create(repository, job_id)
    assert repository.acquire_worker_lease(
        worker.worker_id,
        worker.instance_token,
        lease_seconds=worker.lease_seconds,
    ) is True
    worker._lease_owned = True
    launch_token = f"launch-{job_id}"
    assert repository.claim_next_queued_job(
        worker_id=worker.worker_id,
        instance_token=worker.instance_token,
        launch_token=launch_token,
    ) is not None
    repository.record_process_started(
        job_id,
        worker_id=worker.worker_id,
        instance_token=worker.instance_token,
        launch_token=launch_token,
        pid=pid,
        process_create_time=f"create-{pid}",
        process_group_id=pid,
        command_summary=f"fake-pipeline --job-id {job_id}",
    )
    managed = ManagedProcess(
        job_id=job_id,
        launch_token=launch_token,
        pid=pid,
        process_create_time=f"create-{pid}",
        process_group_id=pid,
        process=process,  # type: ignore[arg-type]
    )
    worker._managed[job_id] = managed
    return managed


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


def test_pipeline_worker_instances_have_unique_tokens_even_with_same_worker_id(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    first = PipelineWorker(repository=repository, worker_id="shared-name")
    second = PipelineWorker(repository=repository, worker_id="shared-name")

    try:
        assert first.worker_id == second.worker_id == "shared-name"
        assert first.instance_token != second.instance_token
        explicit = PipelineWorker(
            repository=repository,
            worker_id="shared-name",
            instance_token="deterministic-token",
        )
        try:
            assert explicit.instance_token == "deterministic-token"
        finally:
            explicit.close()
    finally:
        first.close()
        second.close()


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
        assert process["launch_state"] == "registered"
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


def test_same_worker_id_does_not_allow_two_pipeline_workers_to_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(job_service, "RUNS_DIR", tmp_path / "runs")
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "leased_job")
    builder, started_dir, _, _ = _fake_builder(tmp_path)
    owner = PipelineWorker(
        repository=repository,
        worker_id="shared-name",
        instance_token="owner-token",
        command_builder=builder,
        cancel_grace_seconds=0.2,
    )
    contender = PipelineWorker(
        repository=repository,
        worker_id="shared-name",
        instance_token="contender-token",
        command_builder=builder,
        cancel_grace_seconds=0.2,
    )

    try:
        assert owner.run_once() is True
        assert contender.run_once() is False
        assert _wait_until(lambda: (started_dir / "leased_job").exists())
        assert repository.get_process("leased_job")["worker_id"] == "shared-name"
    finally:
        contender.close()
        _stop_worker_jobs(owner, repository)


def test_reconciliation_quarantines_unregistered_launch_and_never_schedules(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    repository = JobRepository(db_path)
    _create(repository, "job_first")
    _create(repository, "job_second")
    assert repository.acquire_worker_lease("old-worker", "old-token") is True
    repository.claim_next_queued_job(
        worker_id="old-worker",
        instance_token="old-token",
        launch_token="unregistered-launch",
    )
    assert repository.release_worker_lease("old-worker", "old-token") is True
    pipeline_launches: list[str] = []
    kill_calls: list[str] = []

    def forbidden_builder(job: dict[str, object]) -> LaunchSpec:
        pipeline_launches.append(str(job["job_id"]))
        raise AssertionError("identity-unresolved work must block pipeline launch")

    def forbidden_kill(*args, **kwargs):
        kill_calls.append("kill")
        raise AssertionError("reconciliation must not kill an unidentified process")

    monkeypatch.setattr(worker_module, "terminate_verified_process_tree", forbidden_kill)
    monkeypatch.setattr(job_service, "terminate_process_tree", forbidden_kill)
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=forbidden_builder,
    )

    try:
        assert worker.run_once() is True
        first_version = repository.get_job("job_first")["version"]
        for _ in range(2):
            assert worker.run_once() is True

        assert repository.get_job("job_first")["execution_status"] == "running"
        assert repository.get_process("job_first")["launch_state"] == (
            "identity_unresolved"
        )
        assert repository.get_job("job_first")["version"] == first_version
        assert repository.get_job("job_second")["execution_status"] == "queued"
        assert worker.last_reconcile_result.safe_to_schedule is False
        assert worker.last_reconcile_result.unresolved_job_ids == ("job_first",)
        assert pipeline_launches == []
        assert kill_calls == []
        with closing(connect_database(db_path)) as connection:
            event_count = connection.execute(
                """
                SELECT COUNT(*) FROM job_events
                WHERE job_id = ? AND event_type = 'job.launch_identity_unresolved'
                """,
                ("job_first",),
            ).fetchone()[0]
        assert event_count == 1
    finally:
        worker.close()


def test_expected_builder_failure_does_not_stop_worker_or_leak_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    db_path = tmp_path / "paper2code.db"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(db_path)
    _create(repository, "bad_launch")
    _create(repository, "good_launch")
    secret = "FAKE-KEY-MUST-NOT-PERSIST"
    launches: list[str] = []

    class FakeProcess:
        pid = 4801

        @staticmethod
        def poll() -> None:
            return None

    def builder(job: dict[str, object]) -> LaunchSpec:
        job_id = str(job["job_id"])
        launches.append(job_id)
        if job_id == "bad_launch":
            raise ValueError(f"bad fake settings {secret}")
        return LaunchSpec(
            command=["fake-pipeline"],
            environment={"IGNORED_FAKE_KEY": secret},
            command_summary="fake-pipeline --job-id good_launch",
        )

    monkeypatch.setattr(
        worker_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        worker_module,
        "get_process_create_time",
        lambda *args: "create-4801",
    )
    monkeypatch.setattr(job_service, "process_group_id", lambda process: 4801)
    worker = PipelineWorker(
        repository=repository,
        worker_id="recoverable-launch-worker",
        instance_token="recoverable-launch-token",
        command_builder=builder,
    )
    try:
        assert worker.run_once() is True
        bad = repository.get_job("bad_launch")
        assert bad["execution_status"] == "failed"
        assert bad["failure_code"] == "process_launch_failed"
        assert not (runs_dir / "bad_launch" / "logs" / "00_worker_pipeline.log").exists()

        assert worker.run_once() is True
        assert repository.get_job("good_launch")["execution_status"] == "running"
        assert launches == ["bad_launch", "good_launch"]
        with closing(connect_database(db_path)) as connection:
            launch_failure_events = connection.execute(
                "SELECT COUNT(*) FROM job_events "
                "WHERE job_id = 'bad_launch' AND event_type = 'job.process_launch_failed'"
            ).fetchone()[0]
        assert launch_failure_events == 1
    finally:
        worker.close()

    for database_file in tmp_path.glob("paper2code.db*"):
        assert secret.encode() not in database_file.read_bytes()
    if runs_dir.exists():
        for log_file in runs_dir.rglob("*.log"):
            assert secret not in log_file.read_text(encoding="utf-8")


def test_missing_settings_is_job_launch_failure_not_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "settings_missing")
    monkeypatch.setattr(worker_module, "load_settings", lambda: None)
    worker = PipelineWorker(
        repository=repository,
        worker_id="settings-worker",
        instance_token="settings-token",
    )
    try:
        assert worker.run_once() is True
        job = repository.get_job("settings_missing")
        assert job["execution_status"] == "failed"
        assert job["failure_code"] == "process_launch_failed"
    finally:
        worker.close()


def test_worker_fails_closed_before_popen_when_queued_selection_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(
        job_id="provider_selection_changed",
        request=_request(),
        paper_name="paper",
        provider_snapshot=_selection_snapshot(),
    )
    monkeypatch.setattr(
        worker_module,
        "load_settings",
        lambda: _settings_for("deepseek", "deepseek-v4-pro", key_suffix="changed"),
    )
    monkeypatch.setattr(job_service, "resolve_upload", lambda upload_id: tmp_path / "paper.pdf")
    popen_calls: list[str] = []

    def forbidden_popen(*args, **kwargs):
        popen_calls.append("called")
        raise AssertionError("provider settings mismatch must fail before Popen")

    monkeypatch.setattr(worker_module.subprocess, "Popen", forbidden_popen)
    worker = PipelineWorker(
        repository=repository,
        worker_id="provider-selection-worker",
        instance_token="provider-selection-token",
    )

    try:
        assert worker.run_once() is True
        job = repository.get_job("provider_selection_changed")
        assert job["execution_status"] == "failed"
        assert job["failure_code"] == "provider_settings_changed"
        assert popen_calls == []
    finally:
        worker.close()


@pytest.mark.parametrize("snapshot_mode", ["missing", "fingerprint_changed"])
def test_worker_fails_closed_before_popen_for_untrusted_queued_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    snapshot_mode: str,
) -> None:
    repository = JobRepository(tmp_path / f"{snapshot_mode}.db")
    snapshot = None
    if snapshot_mode == "fingerprint_changed":
        snapshot = {
            **_selection_snapshot(),
            "provider_contract_fingerprint": "f" * 64,
        }
    repository.create_job(
        job_id=f"snapshot_{snapshot_mode}",
        request=_request(),
        paper_name="paper",
        provider_snapshot=snapshot,
    )
    monkeypatch.setattr(worker_module, "load_settings", _settings)
    monkeypatch.setattr(job_service, "resolve_upload", lambda upload_id: tmp_path / "paper.pdf")
    popen_calls: list[str] = []
    monkeypatch.setattr(
        worker_module.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append("called"),
    )
    worker = PipelineWorker(
        repository=repository,
        worker_id=f"worker-{snapshot_mode}",
        instance_token=f"token-{snapshot_mode}",
    )

    try:
        assert worker.run_once() is True
        job = repository.get_job(f"snapshot_{snapshot_mode}")
        assert job["execution_status"] == "failed"
        assert job["failure_code"] == "provider_settings_changed"
        assert popen_calls == []
    finally:
        worker.close()


def test_worker_uses_rotated_credentials_for_same_queued_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(
        job_id="rotated_credentials",
        request=_request(),
        paper_name="paper",
        provider_snapshot=_selection_snapshot(),
    )
    job = repository.get_job("rotated_credentials")
    rotated = _settings_for("openai", "gpt-4.1-mini", key_suffix="rotated")
    monkeypatch.setattr(worker_module, "load_settings", lambda: rotated)
    monkeypatch.setattr(job_service, "resolve_upload", lambda upload_id: tmp_path / "paper.pdf")

    launch = worker_module._pipeline_launch_spec(job)

    assert launch.environment["REPRODUCE_API_KEY"] == "fake-reproduce-rotated"
    assert launch.environment["EVAL_API_KEY"] == "fake-evaluation-rotated"
    assert "fake-reproduce-key" not in repr(launch)
    assert "fake-evaluation-key" not in repr(launch)


def test_worker_fails_closed_before_popen_when_rotated_credentials_are_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(
        job_id="rotated_invalid_credentials",
        request=_request(),
        paper_name="paper",
        provider_snapshot=_selection_snapshot(),
    )
    invalid_rotated = _settings_for("openai", "gpt-4.1-mini", key_suffix="rotated")
    invalid_rotated.reproduce.base_url = ""
    monkeypatch.setattr(worker_module, "load_settings", lambda: invalid_rotated)
    monkeypatch.setattr(job_service, "resolve_upload", lambda upload_id: tmp_path / "paper.pdf")
    popen_calls: list[str] = []

    def forbidden_popen(*args, **kwargs):
        popen_calls.append("called")
        raise AssertionError("invalid rotated credentials must fail before Popen")

    monkeypatch.setattr(worker_module.subprocess, "Popen", forbidden_popen)
    worker = PipelineWorker(
        repository=repository,
        worker_id="invalid-rotated-worker",
        instance_token="invalid-rotated-token",
    )

    try:
        assert worker.run_once() is True
        job = repository.get_job("rotated_invalid_credentials")
        assert job["execution_status"] == "failed"
        assert job["failure_code"] == "provider_settings_changed"
        assert popen_calls == []
    finally:
        worker.close()


def test_missing_uploaded_pdf_is_job_launch_failure_not_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(job_service, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(worker_module, "load_settings", _settings)
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(
        job_id="upload_missing",
        request=_request(upload_id="1" * 32),
        paper_name="paper",
        provider_snapshot=_selection_snapshot(),
    )
    worker = PipelineWorker(
        repository=repository,
        worker_id="upload-worker",
        instance_token="upload-token",
    )
    try:
        assert worker.run_once() is True
        job = repository.get_job("upload_missing")
        assert job["execution_status"] == "failed"
        assert job["failure_code"] == "process_launch_failed"
    finally:
        worker.close()


@pytest.mark.parametrize(
    "popen_error",
    [
        FileNotFoundError("fake executable missing"),
        OSError("fake process creation error"),
        ValueError("fake invalid process parameters"),
    ],
)
def test_expected_popen_failure_is_persisted_and_log_handle_is_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    popen_error: Exception,
) -> None:
    monkeypatch.setattr(job_service, "RUNS_DIR", tmp_path / "runs")
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "popen_failure")
    log_handle = io.StringIO()
    monkeypatch.setattr(worker_module, "open", lambda *args, **kwargs: log_handle, raising=False)
    monkeypatch.setattr(
        worker_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(popen_error),
    )
    worker = PipelineWorker(
        repository=repository,
        worker_id="popen-worker",
        instance_token="popen-token",
        command_builder=lambda job: LaunchSpec(
            command=["fake-pipeline"],
            environment={},
            command_summary="fake-pipeline --job-id popen_failure",
        ),
    )
    try:
        assert worker.run_once() is True
        job = repository.get_job("popen_failure")
        assert job["execution_status"] == "failed"
        assert job["failure_code"] == "process_launch_failed"
        assert log_handle.closed is True
    finally:
        worker.close()


@pytest.mark.parametrize(
    "launch_spec",
    [
        LaunchSpec(command=[], environment={}, command_summary="summary"),
        LaunchSpec(command=["fake"], environment={}, command_summary=""),
        LaunchSpec(command=["fake"], environment={}, command_summary="bad\nsummary"),
        LaunchSpec(command=["fake"], environment={}, command_summary="x" * 1025),
    ],
)
def test_invalid_launch_spec_fails_job_before_popen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    launch_spec: LaunchSpec,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "invalid_launch_spec")
    popen_calls: list[str] = []

    def forbidden_popen(*args, **kwargs):
        popen_calls.append("popen")
        raise AssertionError("invalid launch specifications must fail before Popen")

    monkeypatch.setattr(worker_module.subprocess, "Popen", forbidden_popen)
    worker = PipelineWorker(
        repository=repository,
        worker_id="invalid-spec-worker",
        instance_token="invalid-spec-token",
        command_builder=lambda job: launch_spec,
    )
    try:
        assert worker.run_once() is True
        job = repository.get_job("invalid_launch_spec")
        assert job["execution_status"] == "failed"
        assert job["failure_code"] == "process_launch_failed"
        assert popen_calls == []
    finally:
        worker.close()


def test_unexpected_builder_error_and_repository_failure_remain_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unexpected_repository = JobRepository(tmp_path / "unexpected.db")
    _create(unexpected_repository, "unexpected_bug")
    unexpected_worker = PipelineWorker(
        repository=unexpected_repository,
        worker_id="unexpected-worker",
        instance_token="unexpected-token",
        command_builder=lambda job: (_ for _ in ()).throw(
            RuntimeError("unexpected worker bug")
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="unexpected worker bug"):
            unexpected_worker.run_once()
        assert unexpected_repository.get_job("unexpected_bug")["execution_status"] == (
            "running"
        )
    finally:
        unexpected_worker.close()

    database_repository = JobRepository(tmp_path / "database.db")
    _create(database_repository, "database_failure")
    monkeypatch.setattr(
        database_repository,
        "fail_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database unavailable")
        ),
    )
    database_worker = PipelineWorker(
        repository=database_repository,
        worker_id="database-worker",
        instance_token="database-token",
        command_builder=lambda job: (_ for _ in ()).throw(ValueError("bad job")),
    )
    try:
        with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
            database_worker.run_once()
    finally:
        database_worker.close()


def test_lease_loss_while_recording_job_launch_failure_propagates(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    repository = JobRepository(db_path)
    _create(repository, "fenced_launch_failure")

    def lose_lease_then_fail(job: dict[str, object]) -> LaunchSpec:
        with closing(connect_database(db_path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE worker_leases SET expires_at = ? WHERE lease_name = ?",
                    ("2000-01-01T00:00:00.000Z", "pipeline-worker"),
                )
        assert repository.acquire_worker_lease("takeover", "takeover-token") is True
        raise ValueError("job launch validation failed")

    worker = PipelineWorker(
        repository=repository,
        worker_id="fenced-worker",
        instance_token="fenced-token",
        command_builder=lose_lease_then_fail,
    )
    try:
        with pytest.raises(RuntimeError, match="lease"):
            worker.run_once()
        job = repository.get_job("fenced_launch_failure")
        assert job["execution_status"] == "running"
        assert job["failure_code"] is None
    finally:
        worker.close()


@pytest.mark.parametrize("termination_confirmed", [True, False])
def test_process_registration_failure_fails_only_after_confirmed_tree_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    termination_confirmed: bool,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "registration_job")
    _create(repository, "queued_job")
    launches: list[str] = []
    raw_kill_calls: list[int] = []

    class FakeProcess:
        pid = 4321

        @staticmethod
        def poll() -> None:
            return None

    def builder(job: dict[str, object]) -> LaunchSpec:
        launches.append(str(job["job_id"]))
        return LaunchSpec(
            command=["fake-pipeline"],
            environment={},
            command_summary=f"fake-pipeline --job-id {job['job_id']}",
        )

    monkeypatch.setattr(worker_module.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(worker_module, "get_process_create_time", lambda *args: "create-4321")
    monkeypatch.setattr(job_service, "process_group_id", lambda process: 4321)
    monkeypatch.setattr(
        worker_module,
        "terminate_verified_process_tree",
        lambda **kwargs: TerminationResult(
            termination_confirmed,
            (
                "process_tree_terminated"
                if termination_confirmed
                else "process_tree_termination_failed"
            ),
        ),
    )

    def forbidden_raw_kill(pid: int, *args, **kwargs):
        raw_kill_calls.append(pid)
        raise AssertionError("registration cleanup must not use an unverified PID kill")

    monkeypatch.setattr(job_service, "terminate_process_tree", forbidden_raw_kill)
    monkeypatch.setattr(
        repository,
        "record_process_started",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OptimisticLockConflictError()
        ),
    )
    worker = PipelineWorker(
        repository=repository,
        worker_id="registration-worker",
        command_builder=builder,
    )

    try:
        with pytest.raises(OptimisticLockConflictError):
            worker.run_once()

        job = repository.get_job("registration_job")
        process = repository.get_process("registration_job")
        if termination_confirmed:
            assert job["execution_status"] == "failed"
            assert job["failure_code"] == "process_registration_failed"
            assert process["launch_state"] == "exited"
        else:
            assert job["execution_status"] == "running"
            assert process["launch_state"] == "identity_unresolved"
            assert worker.run_once() is True
        assert repository.get_job("queued_job")["execution_status"] == "queued"
        assert launches == ["registration_job"]
        assert raw_kill_calls == []
    finally:
        worker.close()


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


@pytest.mark.parametrize(
    ("exit_code", "expected_status", "expected_failure"),
    [(0, "completed", None), (7, "failed", "pipeline_process_failed")],
)
def test_natural_exit_wins_over_late_cancel_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exit_code: int,
    expected_status: str,
    expected_failure: str | None,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="exit-race-worker",
        instance_token=f"exit-race-{exit_code}",
    )

    class ExitedProcess:
        @staticmethod
        def poll() -> int:
            return exit_code

    _registered_managed_job(
        repository,
        worker,
        f"exit_race_{exit_code}",
        ExitedProcess(),
        pid=4300 + exit_code,
    )
    repository.request_cancel(f"exit_race_{exit_code}")
    termination_calls: list[str] = []

    def forbidden_termination(**kwargs):
        termination_calls.append("terminate")
        raise AssertionError("an already-exited local process must not be canceled")

    monkeypatch.setattr(
        worker_module,
        "terminate_verified_process_tree",
        forbidden_termination,
    )
    try:
        worker._monitor_managed()

        job = repository.get_job(f"exit_race_{exit_code}")
        command = repository.get_cancel_command(f"exit_race_{exit_code}")
        process = repository.get_process(f"exit_race_{exit_code}")
        assert job["execution_status"] == expected_status
        assert job["failure_code"] == expected_failure
        assert process["exit_code"] == exit_code
        assert command["status"] == "failed"
        assert command["error_code"] == "already_finished"
        assert termination_calls == []
        with closing(connect_database(repository.database_path)) as connection:
            canceled_events = connection.execute(
                "SELECT COUNT(*) FROM job_events "
                "WHERE job_id = ? AND event_type = 'job.canceled'",
                (f"exit_race_{exit_code}",),
            ).fetchone()[0]
        assert canceled_events == 0
    finally:
        worker.close()


def test_cancel_missing_result_polls_local_process_again_before_failing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="second-poll-worker",
        instance_token="second-poll-token",
    )

    class ExitDuringCancellation:
        def __init__(self) -> None:
            self.poll_results = iter((None, 0))

        def poll(self) -> int | None:
            return next(self.poll_results)

    _registered_managed_job(
        repository,
        worker,
        "second_poll_race",
        ExitDuringCancellation(),
        pid=4401,
    )
    repository.request_cancel("second_poll_race")
    monkeypatch.setattr(
        worker_module,
        "terminate_verified_process_tree",
        lambda **kwargs: TerminationResult(False, "process_missing"),
    )
    try:
        worker._monitor_managed()

        job = repository.get_job("second_poll_race")
        command = repository.get_cancel_command("second_poll_race")
        assert job["execution_status"] == "completed"
        assert job["failure_code"] is None
        assert repository.get_process("second_poll_race")["exit_code"] == 0
        assert command["status"] == "failed"
        assert command["error_code"] == "already_finished"
    finally:
        worker.close()


def test_live_cancel_completes_only_after_verified_tree_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="verified-cancel-worker",
        instance_token="verified-cancel-token",
    )

    class LiveProcess:
        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(timeout: float) -> int:
            assert timeout == 0.2
            return -15

    _registered_managed_job(
        repository,
        worker,
        "verified_cancel",
        LiveProcess(),
        pid=4501,
    )
    repository.request_cancel("verified_cancel")
    monkeypatch.setattr(
        worker_module,
        "terminate_verified_process_tree",
        lambda **kwargs: TerminationResult(True, "process_tree_terminated"),
    )
    try:
        worker._monitor_managed()

        assert repository.get_job("verified_cancel")["execution_status"] == "canceled"
        assert repository.get_cancel_command("verified_cancel")["status"] == "completed"
        assert repository.get_process("verified_cancel")["exit_code"] == -15
    finally:
        worker.close()


def test_detached_missing_process_remains_conservative_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="detached-missing-worker",
        instance_token="detached-missing-token",
    )
    _registered_managed_job(
        repository,
        worker,
        "detached_missing",
        None,
        pid=4601,
    )
    repository.request_cancel("detached_missing")
    monkeypatch.setattr(
        worker_module,
        "terminate_verified_process_tree",
        lambda **kwargs: TerminationResult(False, "process_missing"),
    )
    try:
        worker._monitor_managed()

        job = repository.get_job("detached_missing")
        assert job["execution_status"] == "failed"
        assert job["failure_code"] == "process_exited_without_checkpoint"
        assert repository.get_cancel_command("detached_missing")["status"] == "failed"
    finally:
        worker.close()


def test_cancel_renews_lease_before_any_termination_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="lost-cancel-worker",
        instance_token="lost-cancel-token",
    )

    class LiveProcess:
        @staticmethod
        def poll() -> None:
            return None

    _registered_managed_job(
        repository,
        worker,
        "lost_cancel_lease",
        LiveProcess(),
        pid=4701,
    )
    repository.request_cancel("lost_cancel_lease")
    monkeypatch.setattr(repository, "acquire_worker_lease", lambda *args, **kwargs: False)
    termination_calls: list[str] = []

    def forbidden_termination(**kwargs):
        termination_calls.append("terminate")
        raise AssertionError("a worker that cannot renew its lease must not signal")

    monkeypatch.setattr(
        worker_module,
        "terminate_verified_process_tree",
        forbidden_termination,
    )
    try:
        with pytest.raises(RuntimeError, match="lease"):
            worker._monitor_managed()
        assert termination_calls == []
        assert repository.get_job("lost_cancel_lease")["execution_status"] == "running"
        assert repository.get_cancel_command("lost_cancel_lease")["status"] == "pending"
    finally:
        worker._lease_owned = False
        worker.close()


@pytest.mark.parametrize(
    "reason",
    ["process_tree_termination_failed", "process_termination_timeout"],
)
def test_unverified_cancel_result_never_writes_canceled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reason: str,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="failed-cancel-worker",
        instance_token=f"failed-cancel-{reason}",
    )

    class LiveProcess:
        @staticmethod
        def poll() -> None:
            return None

    _registered_managed_job(
        repository,
        worker,
        "unverified_cancel",
        LiveProcess(),
        pid=4751,
    )
    repository.request_cancel("unverified_cancel")
    monkeypatch.setattr(
        worker_module,
        "terminate_verified_process_tree",
        lambda **kwargs: TerminationResult(False, reason),
    )
    try:
        with pytest.raises(RuntimeError, match=reason):
            worker._monitor_managed()
        assert repository.get_job("unverified_cancel")["execution_status"] == "running"
        assert repository.get_cancel_command("unverified_cancel")["status"] == "claimed"
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
        assert repository.acquire_worker_lease("old-worker", "old-token") is True
        claimed = repository.claim_next_queued_job(
            worker_id="old-worker",
            instance_token="old-token",
            launch_token="old-launch-token",
        )
        assert claimed is not None
        repository.record_process_started(
            "identity_attack",
            worker_id="old-worker",
            instance_token="old-token",
            launch_token="old-launch-token",
            pid=unrelated.pid,
            process_create_time="definitely-not-the-real-create-time",
            process_group_id=unrelated.pid,
            command_summary="python fake_pipeline.py --job-id identity_attack",
        )
        assert repository.release_worker_lease("old-worker", "old-token") is True
        repository.request_cancel("identity_attack")

        assert worker.run_once() is True
        assert unrelated.poll() is None
        failed = repository.get_job("identity_attack")
        assert failed["execution_status"] == "failed"
        assert failed["failure_code"] == "process_identity_mismatch"
        assert repository.get_process("identity_attack")["launch_state"] == "exited"
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
    assert repository.acquire_worker_lease("old-worker", "old-token") is True
    claimed = repository.claim_next_queued_job(
        worker_id="old-worker",
        instance_token="old-token",
        launch_token="reconcile-launch-token",
    )
    assert claimed is not None
    repository.record_process_started(
        "reconcile_running",
        worker_id="old-worker",
        instance_token="old-token",
        launch_token="reconcile-launch-token",
        pid=process.pid,
        process_create_time=create_time,
        process_group_id=job_service.process_group_id(process),
        command_summary="python fake_pipeline.py --job-id reconcile_running",
    )
    assert repository.release_worker_lease("old-worker", "old-token") is True
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
        assert repository.get_process("reconcile_running")["launch_state"] == (
            "registered"
        )
        assert repository.get_process("reconcile_running")["heartbeat_at"]
    finally:
        worker.close()
        if process.poll() is None:
            job_service.terminate_process_tree(process.pid, process, timeout=3)


def test_startup_reconciliation_marks_dead_process_as_explainable_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(job_service, "RUNS_DIR", tmp_path / "runs")
    repository = JobRepository(tmp_path / "paper2code.db")
    _create(repository, "reconcile_dead")
    _create(repository, "reconcile_next")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **job_service.pipeline_popen_kwargs(),
    )
    create_time = get_process_create_time(process.pid, process)
    assert create_time is not None
    assert repository.acquire_worker_lease("old-worker", "old-token") is True
    repository.claim_next_queued_job(
        worker_id="old-worker",
        instance_token="old-token",
        launch_token="dead-launch-token",
    )
    repository.record_process_started(
        "reconcile_dead",
        worker_id="old-worker",
        instance_token="old-token",
        launch_token="dead-launch-token",
        pid=process.pid,
        process_create_time=create_time,
        process_group_id=job_service.process_group_id(process),
        command_summary="python fake_pipeline.py --job-id reconcile_dead",
    )
    assert repository.release_worker_lease("old-worker", "old-token") is True
    assert job_service.terminate_process_tree(process.pid, process, timeout=3)[0]
    builder, started_dir, _, _ = _fake_builder(tmp_path)
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=builder,
        cancel_grace_seconds=0.2,
    )

    try:
        assert worker.run_once() is True
        failed = repository.get_job("reconcile_dead")
        assert failed["execution_status"] == "failed"
        assert failed["failure_code"] == "process_exited_without_checkpoint"
        assert repository.get_process("reconcile_dead")["launch_state"] == "exited"
        assert repository.get_job("reconcile_next")["execution_status"] == "running"
        assert _wait_until(lambda: (started_dir / "reconcile_next").exists())
    finally:
        _stop_worker_jobs(worker, repository)


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
