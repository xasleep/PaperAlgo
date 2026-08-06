from contextlib import closing
from pathlib import Path

import pytest

from codes.checkpoint_protocol import completed_checkpoint, write_checkpoint
from web_api import job_service, worker as worker_module
from web_api.database import connect_database
from web_api.errors import OptimisticLockConflictError
from web_api.job_repository import JobRepository
from web_api.worker import ManagedProcess, PipelineWorker


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


def _registered_job(repository: JobRepository, job_id: str) -> str:
    repository.create_job(job_id=job_id, request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("old-worker", "old-token")
    launch_token = f"old-launch-{job_id}"
    assert repository.claim_next_queued_job(
        worker_id="old-worker",
        instance_token="old-token",
        launch_token=launch_token,
    ) is not None
    repository.record_process_started(
        job_id,
        worker_id="old-worker",
        instance_token="old-token",
        launch_token=launch_token,
        pid=43210,
        process_create_time="create-43210",
        process_group_id=43210,
        command_summary="python run_pipeline.py --job-id redacted",
    )
    assert repository.release_worker_lease("old-worker", "old-token")
    return launch_token


def _planning_boundary(run_dir: Path, job_id: str) -> None:
    markdown = run_dir / "input" / "source_markdown.md"
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("# paper\n", encoding="utf-8")
    manifest = run_dir / "output" / "task_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        '{"version":1,"files":[{"path":"main.py"}]}', encoding="utf-8"
    )
    (manifest.parent / "planning_response.json").write_text("{}", encoding="utf-8")
    (manifest.parent / "planning_trajectories.json").write_text("[]", encoding="utf-8")
    input_checkpoint = completed_checkpoint(
        run_dir,
        job_id=job_id,
        stage_name="mineru_skipped",
        stage_sequence=1,
        stage_attempt=1,
        started_at="2026-07-22T00:00:00.000Z",
        completed_at="2026-07-22T00:00:01.000Z",
        artifact_paths=["input/source_markdown.md"],
        resume_from_stage="planning",
    )
    write_checkpoint(run_dir, input_checkpoint)
    checkpoint = completed_checkpoint(
        run_dir,
        job_id=job_id,
        stage_name="planning",
        stage_sequence=2,
        stage_attempt=1,
        started_at="2026-07-22T00:00:00.000Z",
        completed_at="2026-07-22T00:00:01.000Z",
        artifact_paths=[
            "input/source_markdown.md",
            "output/task_manifest.json",
            "output/planning_response.json",
            "output/planning_trajectories.json",
        ],
        resume_from_stage="extract_config",
    )
    write_checkpoint(run_dir, checkpoint)


def _completed_boundary(
    run_dir: Path,
    job_id: str,
    *,
    through: str = "completed",
) -> None:
    _planning_boundary(run_dir, job_id)
    paths = [
        "input/source_markdown.md",
        "output/task_manifest.json",
        "output/planning_response.json",
        "output/planning_trajectories.json",
    ]
    additions = [
        (
            "extract_config",
            3,
            "analyzing",
            {
                "output/planning_config.yaml": "seed: 1\n",
                "output/planning_artifacts/1.1_overall_plan.txt": "plan\n",
            },
        ),
        (
            "analyzing",
            4,
            "coding",
            {
                "output/analyzing_artifacts/main_analysis.txt": "analysis\n",
                "output/main_simple_analysis_response.json": "{}",
            },
        ),
        (
            "coding",
            5,
            "evaluation",
            {
                "repo/config.yaml": "seed: 1\n",
                "repo/main.py": "print('ok')\n",
            },
        ),
        (
            "evaluation",
            6,
            "completed",
            {
                "output/repo_status.json": '{"status":"测评且通过"}',
                "output/eval_feedback.json": "{}",
            },
        ),
        (
            "completed",
            7,
            None,
            {
                "run_status.json": '{"status":"completed"}',
                "run_summary.json": '{"status":"passed"}',
            },
        ),
    ]
    for stage_name, sequence, resume_from_stage, files in additions:
        for relative_path, content in files.items():
            path = run_dir / Path(*relative_path.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            paths.append(relative_path)
        write_checkpoint(
            run_dir,
            completed_checkpoint(
                run_dir,
                job_id=job_id,
                stage_name=stage_name,
                stage_sequence=sequence,
                stage_attempt=1,
                started_at=f"2026-07-22T00:00:{sequence:02d}.000Z",
                completed_at=f"2026-07-22T00:00:{sequence:02d}.500Z",
                artifact_paths=paths,
                resume_from_stage=resume_from_stage,
            ),
        )
        if stage_name == through:
            return
    raise AssertionError(f"Unknown completed boundary {through}")


def _online_exited_process(
    repository: JobRepository,
    worker: PipelineWorker,
    job_id: str,
    exit_code: int,
) -> ManagedProcess:
    class ExitedProcess:
        pid = 47017

        @staticmethod
        def poll() -> int:
            return exit_code

    repository.create_job(job_id=job_id, request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease(worker.worker_id, worker.instance_token)
    launch_token = f"online-launch-{job_id}"
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
        pid=ExitedProcess.pid,
        process_create_time=f"create-{job_id}",
        process_group_id=ExitedProcess.pid,
        command_summary="python run_pipeline.py --job-id redacted",
    )
    managed = ManagedProcess(
        job_id=job_id,
        launch_token=launch_token,
        pid=ExitedProcess.pid,
        process_create_time=f"create-{job_id}",
        process_group_id=ExitedProcess.pid,
        process=ExitedProcess(),  # type: ignore[arg-type]
    )
    worker._managed[job_id] = managed
    worker._reconciled = True
    return managed


def test_online_nonzero_exit_recovers_once_from_completed_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="online-recovery-worker",
        instance_token="online-recovery-token",
        command_builder=lambda job: None,  # type: ignore[arg-type,return-value]
    )
    managed = _online_exited_process(repository, worker, "online_exit_17", 17)
    _planning_boundary(runs_dir / managed.job_id, managed.job_id)
    launches: list[tuple[dict[str, object], str]] = []
    monkeypatch.setattr(
        worker,
        "_launch",
        lambda job, token: launches.append((dict(job), token)),
    )

    try:
        worker._monitor_managed()
    finally:
        worker.close()

    job = repository.get_job(managed.job_id)
    process = repository.get_process(managed.job_id)
    assert job["execution_status"] == "running"
    assert job["failure_code"] is None
    assert job["recovery_count"] == 1
    assert job["recovery_status"] == "prepared"
    assert process is not None
    assert process["launch_token"] != managed.launch_token
    assert process["process_attempt"] == 2
    assert len(launches) == 1
    assert launches[0][1] == process["launch_token"]
    with closing(connect_database(repository.database_path)) as connection:
        history = connection.execute(
            "SELECT * FROM job_process_history WHERE job_id = ?",
            (managed.job_id,),
        ).fetchall()
    assert len(history) == 1
    assert history[0]["launch_token"] == managed.launch_token
    assert history[0]["exit_code"] == 17


def test_online_zero_exit_completes_without_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="online-zero-worker",
        instance_token="online-zero-token",
    )
    managed = _online_exited_process(repository, worker, "online_exit_0", 0)
    _planning_boundary(runs_dir / managed.job_id, managed.job_id)

    try:
        worker._monitor_managed()
    finally:
        worker.close()

    job = repository.get_job(managed.job_id)
    assert job["execution_status"] == "completed"
    assert job["recovery_count"] == 0
    assert repository.get_process(managed.job_id)["exit_code"] == 0


def test_online_nonzero_exit_with_late_cancel_never_recovers_or_cancels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="online-late-cancel-worker",
        instance_token="online-late-cancel-token",
    )
    managed = _online_exited_process(repository, worker, "online_late_cancel", 17)
    _planning_boundary(runs_dir / managed.job_id, managed.job_id)
    repository.request_cancel(managed.job_id)

    try:
        worker._monitor_managed()
    finally:
        worker.close()

    job = repository.get_job(managed.job_id)
    command = repository.get_cancel_command(managed.job_id)
    assert job["execution_status"] == "failed"
    assert job["failure_code"] == "pipeline_process_failed"
    assert job["recovery_count"] == 0
    assert command is not None
    assert command["status"] == "failed"
    assert command["error_code"] == "already_finished"
    with closing(connect_database(repository.database_path)) as connection:
        history_count = connection.execute(
            "SELECT COUNT(*) FROM job_process_history WHERE job_id = ?",
            (managed.job_id,),
        ).fetchone()[0]
    assert history_count == 0


def test_online_nonzero_exit_without_checkpoint_fails_without_recovery(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="online-no-checkpoint-worker",
        instance_token="online-no-checkpoint-token",
    )
    managed = _online_exited_process(repository, worker, "online_no_checkpoint", 17)

    try:
        worker._monitor_managed()
    finally:
        worker.close()

    job = repository.get_job(managed.job_id)
    assert job["execution_status"] == "failed"
    assert job["failure_code"] == "process_exited_without_checkpoint"
    assert job["recovery_count"] == 0
    assert repository.get_process(managed.job_id)["process_attempt"] == 1


def test_online_nonzero_exit_with_exhausted_budget_never_creates_third_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    worker = PipelineWorker(
        repository=repository,
        worker_id="online-budget-worker",
        instance_token="online-budget-token",
    )
    managed = _online_exited_process(repository, worker, "online_budget", 17)
    _planning_boundary(runs_dir / managed.job_id, managed.job_id)
    with closing(connect_database(repository.database_path)) as connection:
        connection.execute(
            "UPDATE jobs SET recovery_count = 1, recovery_status = 'running' "
            "WHERE job_id = ?",
            (managed.job_id,),
        )
        connection.execute(
            "UPDATE job_processes SET process_attempt = 2 WHERE job_id = ?",
            (managed.job_id,),
        )
        connection.commit()

    try:
        worker._monitor_managed()
    finally:
        worker.close()

    job = repository.get_job(managed.job_id)
    process = repository.get_process(managed.job_id)
    assert job["execution_status"] == "failed"
    assert job["failure_code"] == "recovery_attempts_exhausted"
    assert job["recovery_count"] == 1
    assert process is not None and process["process_attempt"] == 2


def test_dead_registered_process_recovers_once_with_new_launch_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    old_launch = _registered_job(repository, "recover_job")
    _planning_boundary(runs_dir / "recover_job", "recover_job")
    launches: list[tuple[dict[str, object], str]] = []
    monkeypatch.setattr(worker_module, "process_identity_status", lambda *args: "missing")
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=lambda job: None,  # type: ignore[arg-type,return-value]
    )
    monkeypatch.setattr(
        worker,
        "_launch",
        lambda job, launch_token: launches.append((dict(job), launch_token)),
    )

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    job = repository.get_job("recover_job")
    process = repository.get_process("recover_job")
    assert job["execution_status"] == "running"
    assert job["recovery_count"] == 1
    assert job["recovery_status"] == "prepared"
    assert process is not None
    assert process["launch_token"] != old_launch
    assert process["launch_state"] == "claimed"
    assert process["process_attempt"] == 2
    assert len(launches) == 1
    assert launches[0][0]["_resume_from_stage"] == "extract_config"
    assert launches[0][0]["_resume_stage_sequence"] == 3
    assert launches[0][0]["_recovery_count"] == 1
    assert launches[0][1] == process["launch_token"]
    with closing(connect_database(repository.database_path)) as connection:
        history = connection.execute(
            "SELECT * FROM job_process_history WHERE job_id = ?",
            ("recover_job",),
        ).fetchall()
    assert len(history) == 1
    assert history[0]["launch_token"] == old_launch


def test_invalid_checkpoint_fails_closed_and_never_launches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    _registered_job(repository, "invalid_checkpoint")
    checkpoint_dir = runs_dir / "invalid_checkpoint" / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "checkpoint-0002-planning-001.json").write_text(
        '{"version":999}', encoding="utf-8"
    )
    launches: list[str] = []
    monkeypatch.setattr(worker_module, "process_identity_status", lambda *args: "missing")
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=lambda job: launches.append(str(job["job_id"])),  # type: ignore[arg-type]
    )

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    job = repository.get_job("invalid_checkpoint")
    assert job["execution_status"] == "failed"
    assert job["failure_code"] == "checkpoint_unsupported_version"
    assert launches == []


@pytest.mark.parametrize("attack", ["manifest", "markdown"])
def test_replaced_planning_dependency_fails_before_recovery_prepare_or_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    attack: str,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    _registered_job(repository, "planning_attack")
    run_dir = runs_dir / "planning_attack"
    _planning_boundary(run_dir, "planning_attack")
    if attack == "manifest":
        (run_dir / "output" / "task_manifest.json").write_text(
            '{"version":1,"files":[{"path":"replacement.py"}]}',
            encoding="utf-8",
        )
    else:
        (run_dir / "input" / "source_markdown.md").write_text(
            "# replaced\n", encoding="utf-8"
        )
    builds: list[str] = []
    monkeypatch.setattr(worker_module, "process_identity_status", lambda *args: "missing")
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=lambda job: builds.append(str(job["job_id"])),  # type: ignore[arg-type]
    )

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    job = repository.get_job("planning_attack")
    assert job["execution_status"] == "failed"
    assert job["failure_code"] in {
        "checkpoint_artifact_mismatch",
        "checkpoint_state_closure_invalid",
    }
    assert job["recovery_count"] == 0
    assert builds == []
    with closing(connect_database(repository.database_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_process_history WHERE job_id = ?",
            ("planning_attack",),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("attack", ["replace", "delete"])
def test_evaluation_repo_attack_fails_before_recovery_prepare_or_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    attack: str,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    _registered_job(repository, "evaluation_attack")
    run_dir = runs_dir / "evaluation_attack"
    _completed_boundary(run_dir, "evaluation_attack", through="evaluation")
    repo_member = run_dir / "repo" / "main.py"
    if attack == "replace":
        repo_member.write_text("print('replaced')\n", encoding="utf-8")
    else:
        repo_member.unlink()
    builds: list[str] = []
    monkeypatch.setattr(worker_module, "process_identity_status", lambda *args: "missing")
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=lambda job: builds.append(str(job["job_id"])),  # type: ignore[arg-type]
    )

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    job = repository.get_job("evaluation_attack")
    assert job["execution_status"] == "failed"
    assert job["failure_code"] in {
        "checkpoint_artifact_missing",
        "checkpoint_artifact_mismatch",
        "checkpoint_state_closure_invalid",
    }
    assert job["recovery_count"] == 0
    assert builds == []
    with closing(connect_database(repository.database_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_process_history WHERE job_id = ?",
            ("evaluation_attack",),
        ).fetchone()[0] == 0


def test_recovery_budget_is_persistent_and_prevents_second_relaunch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    old_launch = _registered_job(repository, "budget_job")
    _planning_boundary(runs_dir / "budget_job", "budget_job")
    assert repository.acquire_worker_lease("prepare-worker", "prepare-token")
    repository.record_completed_checkpoint(
        "budget_job",
        worker_id="prepare-worker",
        instance_token="prepare-token",
        launch_token=old_launch,
        checkpoint={
            "version": 1,
            "stage_name": "planning",
            "stage_sequence": 2,
            "stage_attempt": 1,
            "status": "completed",
            "resume_from_stage": "extract_config",
        },
        checkpoint_path="checkpoints/checkpoint-0002-planning-001.json",
    )
    prepared = repository.prepare_recovery_attempt(
        "budget_job",
        worker_id="prepare-worker",
        instance_token="prepare-token",
        launch_token=old_launch,
        new_launch_token="second-launch",
        resume_from_stage="extract_config",
        resume_stage_sequence=3,
        max_recoveries=1,
    )
    repeated = repository.prepare_recovery_attempt(
        "budget_job",
        worker_id="prepare-worker",
        instance_token="prepare-token",
        launch_token=old_launch,
        new_launch_token="second-launch",
        resume_from_stage="extract_config",
        resume_stage_sequence=3,
        max_recoveries=1,
    )
    assert prepared["recovery_count"] == repeated["recovery_count"] == 1
    with pytest.raises(OptimisticLockConflictError):
        repository.record_completed_checkpoint(
            "budget_job",
            worker_id="prepare-worker",
            instance_token="prepare-token",
            launch_token=old_launch,
            checkpoint={
                "version": 1,
                "stage_name": "extract_config",
                "stage_sequence": 3,
                "stage_attempt": 3,
                "status": "completed",
                "resume_from_stage": "analyzing",
            },
            checkpoint_path="checkpoints/checkpoint-0003-extract_config-003.json",
        )
    with closing(connect_database(repository.database_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_process_history WHERE job_id = ?",
            ("budget_job",),
        ).fetchone()[0] == 1
    repository.record_process_started(
        "budget_job",
        worker_id="prepare-worker",
        instance_token="prepare-token",
        launch_token="second-launch",
        pid=43211,
        process_create_time="create-43211",
        process_group_id=43211,
        command_summary="python run_pipeline.py --job-id redacted",
    )
    assert repository.release_worker_lease("prepare-worker", "prepare-token")
    launches: list[str] = []
    monkeypatch.setattr(worker_module, "process_identity_status", lambda *args: "missing")
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=lambda job: launches.append(str(job["job_id"])),  # type: ignore[arg-type]
    )

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    job = repository.get_job("budget_job")
    assert job["execution_status"] == "failed"
    assert job["failure_code"] == "recovery_attempts_exhausted"
    assert job["recovery_count"] == 1
    assert launches == []


def test_pending_cancel_blocks_recovery_and_completes_after_confirmed_death(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    _registered_job(repository, "cancel_race")
    _planning_boundary(runs_dir / "cancel_race", "cancel_race")
    repository.request_cancel("cancel_race")
    launches: list[str] = []
    monkeypatch.setattr(worker_module, "process_identity_status", lambda *args: "missing")
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=lambda job: launches.append(str(job["job_id"])),  # type: ignore[arg-type]
    )

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    assert repository.get_job("cancel_race")["execution_status"] == "canceled"
    command = repository.get_cancel_command("cancel_race")
    assert command is not None
    assert command["status"] == "completed"
    assert launches == []


def test_completed_boundary_finishes_confirmed_dead_process_without_relaunch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    _registered_job(repository, "completed_job")
    run_dir = runs_dir / "completed_job"
    _completed_boundary(run_dir, "completed_job")
    launches: list[str] = []
    monkeypatch.setattr(worker_module, "process_identity_status", lambda *args: "missing")
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=lambda job: launches.append(str(job["job_id"])),  # type: ignore[arg-type]
    )

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    job = repository.get_job("completed_job")
    assert job["execution_status"] == "completed"
    assert job["failure_code"] is None
    assert job["recovery_count"] == 0
    assert launches == []


def test_cancel_after_recovery_prepare_is_consumed_before_new_popen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    _registered_job(repository, "cancel_prelaunch")
    _planning_boundary(runs_dir / "cancel_prelaunch", "cancel_prelaunch")
    builds: list[str] = []

    def forbidden_builder(job):
        builds.append(str(job["job_id"]))
        raise AssertionError("cancel must be consumed before command construction")

    monkeypatch.setattr(worker_module, "process_identity_status", lambda *args: "missing")
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=forbidden_builder,
    )
    launch_after_prepare = worker._launch

    def request_cancel_then_launch(job, launch_token):
        repository.request_cancel(str(job["job_id"]))
        launch_after_prepare(job, launch_token)

    monkeypatch.setattr(worker, "_launch", request_cancel_then_launch)

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    job = repository.get_job("cancel_prelaunch")
    command = repository.get_cancel_command("cancel_prelaunch")
    assert job["execution_status"] == "canceled"
    assert job["recovery_count"] == 1
    assert command is not None and command["status"] == "completed"
    assert builds == []


def test_missing_recovery_settings_fail_before_attempt_is_prepared(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(worker_module, "load_settings", lambda: None)
    repository = JobRepository(tmp_path / "paper2code.db")
    _registered_job(repository, "missing_settings")
    _planning_boundary(runs_dir / "missing_settings", "missing_settings")
    monkeypatch.setattr(worker_module, "process_identity_status", lambda *args: "missing")
    worker = PipelineWorker(repository=repository, worker_id="recovery-worker")

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    job = repository.get_job("missing_settings")
    assert job["execution_status"] == "failed"
    assert job["failure_code"] == "checkpoint_recovery_prerequisite_missing"
    assert job["recovery_count"] == 0
    with closing(connect_database(repository.database_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_process_history WHERE job_id = ?",
            ("missing_settings",),
        ).fetchone()[0] == 0


def test_two_workers_create_only_one_recovery_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    _registered_job(repository, "competing_recovery")
    _planning_boundary(runs_dir / "competing_recovery", "competing_recovery")
    launches: list[tuple[str, str]] = []
    monkeypatch.setattr(worker_module, "process_identity_status", lambda *args: "missing")
    workers = [
        PipelineWorker(
            repository=repository,
            worker_id=worker_id,
            command_builder=lambda job: None,  # type: ignore[arg-type,return-value]
        )
        for worker_id in ("recovery-a", "recovery-b")
    ]
    for worker in workers:
        monkeypatch.setattr(
            worker,
            "_launch",
            lambda job, token, worker=worker: launches.append((worker.worker_id, token)),
        )

    try:
        assert workers[0].run_once() is True
        assert workers[1].run_once() is False
    finally:
        for worker in workers:
            worker.close()

    assert len(launches) == 1
    assert repository.get_job("competing_recovery")["recovery_count"] == 1
    with closing(connect_database(repository.database_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_process_history WHERE job_id = ?",
            ("competing_recovery",),
        ).fetchone()[0] == 1


def test_identity_unresolved_never_uses_even_a_valid_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    repository = JobRepository(tmp_path / "paper2code.db")
    for job_id in ("unresolved_with_checkpoint", "queued_after_unresolved"):
        repository.create_job(job_id=job_id, request=_request(), paper_name="paper")
    assert repository.acquire_worker_lease("old-worker", "old-token")
    repository.claim_next_queued_job(
        worker_id="old-worker",
        instance_token="old-token",
        launch_token="unresolved-launch",
    )
    assert repository.release_worker_lease("old-worker", "old-token")
    _planning_boundary(
        runs_dir / "unresolved_with_checkpoint", "unresolved_with_checkpoint"
    )
    builds: list[str] = []
    kills: list[object] = []
    monkeypatch.setattr(
        worker_module,
        "terminate_verified_process_tree",
        lambda **kwargs: kills.append(kwargs),
    )
    worker = PipelineWorker(
        repository=repository,
        worker_id="recovery-worker",
        command_builder=lambda job: builds.append(str(job["job_id"])),  # type: ignore[arg-type]
    )

    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    assert repository.get_job("unresolved_with_checkpoint")["execution_status"] == "running"
    assert repository.get_process("unresolved_with_checkpoint")["launch_state"] == (
        "identity_unresolved"
    )
    assert repository.get_job("queued_after_unresolved")["execution_status"] == "queued"
    assert builds == []
    assert kills == []
