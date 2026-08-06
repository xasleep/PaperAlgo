import json
import os
from pathlib import Path

import pytest

from codes.checkpoint_protocol import (
    CHECKPOINT_MAX_BYTES,
    CheckpointProtocolError,
    completed_checkpoint,
    failed_checkpoint,
    find_recovery_checkpoint,
    latest_completed_checkpoint,
    list_checkpoints,
    read_checkpoint,
    running_checkpoint,
    write_checkpoint,
)


def _artifact(run_dir: Path, relative_path: str, content: str = "stable") -> Path:
    path = run_dir / Path(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _completed(
    run_dir: Path,
    *,
    job_id: str = "job_1",
    stage_name: str = "planning",
    stage_sequence: int = 2,
    stage_attempt: int = 1,
    resume_from_stage: str | None = "extract_config",
    artifact_path: str = "output/task_manifest.json",
) -> Path:
    _artifact(run_dir, "input/source_markdown.md", "# paper\n")
    _artifact(run_dir, artifact_path, '{"version":1,"files":["main.py"]}')
    _artifact(run_dir, "output/planning_response.json", "{}")
    _artifact(run_dir, "output/planning_trajectories.json", "[]")
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
        stage_name=stage_name,
        stage_sequence=stage_sequence,
        stage_attempt=stage_attempt,
        started_at="2026-07-22T00:00:00.000Z",
        completed_at="2026-07-22T00:00:01.000Z",
        artifact_paths=[
            "input/source_markdown.md",
            artifact_path,
            "output/planning_response.json",
            "output/planning_trajectories.json",
        ],
        resume_from_stage=resume_from_stage,
    )
    return write_checkpoint(run_dir, checkpoint)


def _state_files(run_dir: Path) -> dict[str, list[str]]:
    files = {
        "input": ["input/source_markdown.md"],
        "planning": [
            "input/source_markdown.md",
            "output/task_manifest.json",
            "output/planning_response.json",
            "output/planning_trajectories.json",
        ],
        "extract_config": [
            "input/source_markdown.md",
            "output/task_manifest.json",
            "output/planning_response.json",
            "output/planning_trajectories.json",
            "output/planning_config.yaml",
            "output/planning_artifacts/1.1_overall_plan.txt",
        ],
        "analyzing": [
            "input/source_markdown.md",
            "output/task_manifest.json",
            "output/planning_response.json",
            "output/planning_trajectories.json",
            "output/planning_config.yaml",
            "output/planning_artifacts/1.1_overall_plan.txt",
            "output/analyzing_artifacts/main_analysis.txt",
            "output/main_simple_analysis_response.json",
        ],
        "coding": [
            "input/source_markdown.md",
            "output/task_manifest.json",
            "output/planning_response.json",
            "output/planning_trajectories.json",
            "output/planning_config.yaml",
            "output/planning_artifacts/1.1_overall_plan.txt",
            "output/analyzing_artifacts/main_analysis.txt",
            "output/main_simple_analysis_response.json",
            "repo/config.yaml",
            "repo/main.py",
        ],
        "evaluation": [
            "input/source_markdown.md",
            "output/task_manifest.json",
            "output/planning_response.json",
            "output/planning_trajectories.json",
            "output/planning_config.yaml",
            "output/planning_artifacts/1.1_overall_plan.txt",
            "output/analyzing_artifacts/main_analysis.txt",
            "output/main_simple_analysis_response.json",
            "repo/config.yaml",
            "repo/main.py",
            "output/repo_status.json",
            "output/eval_feedback.json",
        ],
    }
    contents = {
        "input/source_markdown.md": "# paper\n",
        "output/task_manifest.json": json.dumps(
            {"version": 1, "files": [{"path": "main.py"}]}
        ),
        "output/planning_response.json": "{}",
        "output/planning_trajectories.json": "[]",
        "output/planning_config.yaml": "seed: 1\n",
        "output/planning_artifacts/1.1_overall_plan.txt": "plan\n",
        "output/analyzing_artifacts/main_analysis.txt": "analysis\n",
        "output/main_simple_analysis_response.json": "{}",
        "repo/config.yaml": "seed: 1\n",
        "repo/main.py": "print('ok')\n",
        "output/repo_status.json": '{"status":"测评且通过"}',
        "output/eval_feedback.json": "{}",
    }
    for path, content in contents.items():
        _artifact(run_dir, path, content)
    return files


def _write_completed_stage(
    run_dir: Path,
    *,
    job_id: str,
    stage_name: str,
    sequence: int,
    artifacts: list[str],
    resume_from_stage: str | None,
    attempt: int = 1,
) -> Path:
    return write_checkpoint(
        run_dir,
        completed_checkpoint(
            run_dir,
            job_id=job_id,
            stage_name=stage_name,
            stage_sequence=sequence,
            stage_attempt=attempt,
            started_at=f"2026-07-22T00:00:{sequence:02d}.000Z",
            completed_at=f"2026-07-22T00:00:{sequence:02d}.500Z",
            artifact_paths=artifacts,
            resume_from_stage=resume_from_stage,
        ),
    )


def _write_chain(
    run_dir: Path,
    *,
    through: str,
    latest_uses_full_closure: bool = True,
    job_id: str = "job_1",
) -> dict[str, list[str]]:
    state = _state_files(run_dir)
    stages = [
        ("mineru_skipped", 1, "input", "planning"),
        ("planning", 2, "planning", "extract_config"),
        ("extract_config", 3, "extract_config", "analyzing"),
        ("analyzing", 4, "analyzing", "coding"),
        ("coding", 5, "coding", "evaluation"),
        ("evaluation", 6, "evaluation", "completed"),
    ]
    own_artifacts = {
        "input": ["input/source_markdown.md"],
        "planning": [
            "output/task_manifest.json",
            "output/planning_response.json",
            "output/planning_trajectories.json",
        ],
        "extract_config": [
            "output/planning_config.yaml",
            "output/planning_artifacts/1.1_overall_plan.txt",
        ],
        "analyzing": [
            "output/analyzing_artifacts/main_analysis.txt",
            "output/main_simple_analysis_response.json",
        ],
        "coding": ["repo/config.yaml", "repo/main.py"],
        "evaluation": ["output/repo_status.json", "output/eval_feedback.json"],
    }
    for stage_name, sequence, state_key, resume in stages:
        artifacts = state[state_key]
        if stage_name == through and not latest_uses_full_closure:
            artifacts = own_artifacts[state_key]
        _write_completed_stage(
            run_dir,
            job_id=job_id,
            stage_name=stage_name,
            sequence=sequence,
            artifacts=artifacts,
            resume_from_stage=resume,
        )
        if stage_name == through:
            return state
    raise AssertionError(f"Unknown terminal stage {through}")


def test_checkpoint_round_trip_is_fixed_schema_and_selects_completed_boundary(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "job_1"
    path = _completed(run_dir)

    loaded = read_checkpoint(path, run_dir=run_dir, expected_job_id="job_1")
    recovery = find_recovery_checkpoint(run_dir, expected_job_id="job_1")

    assert set(loaded) == {
        "version",
        "job_id",
        "stage_name",
        "stage_sequence",
        "stage_attempt",
        "status",
        "started_at",
        "completed_at",
        "resume_from_stage",
        "error_code",
        "artifacts",
    }
    assert loaded["status"] == "completed"
    assert recovery is not None
    assert recovery.checkpoint["stage_name"] == "planning"
    assert recovery.resume_from_stage == "extract_config"
    assert recovery.resume_sequence == 3


@pytest.mark.parametrize(
    ("mutator", "error_code"),
    [
        (lambda value: value.update(version=99), "checkpoint_unsupported_version"),
        (lambda value: value.update(secret="do-not-accept"), "checkpoint_schema_invalid"),
        (lambda value: value.update(stage_name="shell"), "checkpoint_stage_invalid"),
        (lambda value: value.update(stage_sequence=99), "checkpoint_stage_order_invalid"),
        (
            lambda value: value["artifacts"][0].update(path="../../outside.txt"),
            "checkpoint_artifact_path_invalid",
        ),
        (
            lambda value: value["artifacts"][0].update(path="C:/outside.txt"),
            "checkpoint_artifact_path_invalid",
        ),
        (
            lambda value: value["artifacts"][0].update(path="output\\task_manifest.json"),
            "checkpoint_artifact_path_invalid",
        ),
    ],
)
def test_checkpoint_rejects_unknown_fields_versions_stages_and_paths(
    tmp_path: Path,
    mutator,
    error_code: str,
) -> None:
    run_dir = tmp_path / "run"
    path = _completed(run_dir)
    value = json.loads(path.read_text(encoding="utf-8"))
    mutator(value)
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(CheckpointProtocolError) as raised:
        read_checkpoint(path, run_dir=run_dir, expected_job_id="job_1")

    assert raised.value.code == error_code


def test_checkpoint_rejects_oversize_invalid_utf8_and_fingerprint_mismatch(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    path = _completed(run_dir)
    path.write_bytes(b"{" + b" " * CHECKPOINT_MAX_BYTES + b"}")
    with pytest.raises(CheckpointProtocolError) as oversized:
        read_checkpoint(path, run_dir=run_dir, expected_job_id="job_1")
    assert oversized.value.code == "checkpoint_too_large"

    path.write_bytes(b"\xff\xfe")
    with pytest.raises(CheckpointProtocolError) as invalid_utf8:
        read_checkpoint(path, run_dir=run_dir, expected_job_id="job_1")
    assert invalid_utf8.value.code == "checkpoint_invalid_utf8"

    path = _completed(run_dir)
    _artifact(run_dir, "output/task_manifest.json", "tampered")
    with pytest.raises(CheckpointProtocolError) as mismatch:
        find_recovery_checkpoint(run_dir, expected_job_id="job_1")
    assert mismatch.value.code == "checkpoint_artifact_mismatch"


def test_checkpoint_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    path = _completed(run_dir)
    payload = path.read_text(encoding="utf-8")
    path.write_text(payload.replace('{"artifacts"', '{"version":1,"artifacts"', 1), encoding="utf-8")

    with pytest.raises(CheckpointProtocolError) as duplicate:
        read_checkpoint(path, run_dir=run_dir, expected_job_id="job_1")

    assert duplicate.value.code == "checkpoint_schema_invalid"


def test_partial_checkpoint_uses_previous_completed_boundary_and_is_at_least_once(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    _completed(run_dir)
    partial = running_checkpoint(
        job_id="job_1",
        stage_name="extract_config",
        stage_sequence=3,
        stage_attempt=1,
        started_at="2026-07-22T00:00:02.000Z",
    )
    write_checkpoint(run_dir, partial)

    recovery = find_recovery_checkpoint(run_dir, expected_job_id="job_1")

    assert recovery is not None
    assert recovery.resume_from_stage == "extract_config"
    assert recovery.resume_sequence == 3
    assert recovery.resume_attempt == 2


def test_checkpoint_rejects_symlink_and_hardlink_artifacts_when_supported(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    outside = _artifact(tmp_path, "outside.txt", "outside")
    artifact = run_dir / "output" / "linked.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    try:
        artifact.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this Windows host.")

    with pytest.raises(CheckpointProtocolError) as symlinked:
        completed_checkpoint(
            run_dir,
            job_id="job_1",
            stage_name="planning",
            stage_sequence=2,
            stage_attempt=1,
            started_at="2026-07-22T00:00:00.000Z",
            completed_at="2026-07-22T00:00:01.000Z",
            artifact_paths=["output/linked.txt"],
            resume_from_stage="extract_config",
        )
    assert symlinked.value.code == "checkpoint_artifact_unsafe"

    artifact.unlink()
    try:
        os.link(outside, artifact)
    except OSError:
        pytest.skip("Hardlink creation is unavailable on this host.")
    with pytest.raises(CheckpointProtocolError) as hardlinked:
        completed_checkpoint(
            run_dir,
            job_id="job_1",
            stage_name="planning",
            stage_sequence=2,
            stage_attempt=1,
            started_at="2026-07-22T00:00:00.000Z",
            completed_at="2026-07-22T00:00:01.000Z",
            artifact_paths=["output/linked.txt"],
            resume_from_stage="extract_config",
        )
    assert hardlinked.value.code == "checkpoint_artifact_unsafe"


def test_atomic_checkpoint_write_preserves_previous_file_on_replace_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    path = _completed(run_dir)
    before = path.read_bytes()
    checkpoint = running_checkpoint(
        job_id="job_1",
        stage_name="planning",
        stage_sequence=2,
        stage_attempt=1,
        started_at="2026-07-22T00:00:03.000Z",
    )

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        write_checkpoint(run_dir, checkpoint)

    assert path.read_bytes() == before
    assert not list((run_dir / "checkpoints").glob("*.tmp"))


def test_checkpoint_fingerprints_do_not_copy_secret_or_model_payload(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    secret = "FAKE-API-KEY-UNIQUE"
    prompt = "FULL-PROMPT-UNIQUE"
    response = "FULL-MODEL-RESPONSE-UNIQUE"
    _artifact(
        run_dir,
        "output/task_manifest.json",
        json.dumps({"secret": secret, "prompt": prompt, "response": response}),
    )
    checkpoint_value = completed_checkpoint(
        run_dir,
        job_id="job_1",
        stage_name="planning",
        stage_sequence=2,
        stage_attempt=1,
        started_at="2026-07-22T00:00:00.000Z",
        completed_at="2026-07-22T00:00:01.000Z",
        artifact_paths=["output/task_manifest.json"],
        resume_from_stage="extract_config",
    )
    path = write_checkpoint(run_dir, checkpoint_value)
    payload = path.read_text(encoding="utf-8")

    assert secret not in payload
    assert prompt not in payload
    assert response not in payload
    checkpoint = json.loads(payload)
    assert set(checkpoint["artifacts"][0]) == {"path", "size", "sha256"}


def test_planning_recovery_rejects_structurally_valid_replaced_manifest(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    _write_chain(run_dir, through="planning", latest_uses_full_closure=False)
    _artifact(
        run_dir,
        "output/task_manifest.json",
        json.dumps({"version": 1, "files": [{"path": "replacement.py"}]}),
    )

    with pytest.raises(CheckpointProtocolError) as rejected:
        find_recovery_checkpoint(run_dir, expected_job_id="job_1")

    assert rejected.value.code in {
        "checkpoint_artifact_mismatch",
        "checkpoint_state_closure_invalid",
    }


def test_planning_recovery_rejects_replaced_stage_one_markdown(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    _write_chain(run_dir, through="planning", latest_uses_full_closure=False)
    _artifact(run_dir, "input/source_markdown.md", "# replaced\n")

    with pytest.raises(CheckpointProtocolError) as rejected:
        find_recovery_checkpoint(run_dir, expected_job_id="job_1")

    assert rejected.value.code in {
        "checkpoint_artifact_mismatch",
        "checkpoint_state_closure_invalid",
    }


def test_extract_config_recovery_rejects_replaced_planning_output(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    _write_chain(run_dir, through="extract_config", latest_uses_full_closure=False)
    _artifact(run_dir, "output/planning_response.json", '{"replaced":true}')

    with pytest.raises(CheckpointProtocolError) as rejected:
        find_recovery_checkpoint(run_dir, expected_job_id="job_1")

    assert rejected.value.code in {
        "checkpoint_artifact_mismatch",
        "checkpoint_state_closure_invalid",
    }


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (
            "output/task_manifest.json",
            json.dumps({"version": 1, "files": [{"path": "replacement.py"}]}),
        ),
        ("output/planning_config.yaml", "seed: 999\n"),
    ],
)
def test_analyzing_recovery_rejects_replaced_planning_dependency(
    tmp_path: Path,
    path: str,
    replacement: str,
) -> None:
    run_dir = tmp_path / "run"
    _write_chain(run_dir, through="analyzing", latest_uses_full_closure=False)
    _artifact(run_dir, path, replacement)

    with pytest.raises(CheckpointProtocolError) as rejected:
        find_recovery_checkpoint(run_dir, expected_job_id="job_1")

    assert rejected.value.code in {
        "checkpoint_artifact_mismatch",
        "checkpoint_state_closure_invalid",
    }


@pytest.mark.parametrize("attack", ["replace", "delete"])
def test_evaluation_recovery_rejects_repo_member_attack(
    tmp_path: Path,
    attack: str,
) -> None:
    run_dir = tmp_path / "run"
    _write_chain(run_dir, through="evaluation", latest_uses_full_closure=False)
    repo_member = run_dir / "repo" / "main.py"
    if attack == "replace":
        repo_member.write_text("print('replaced')\n", encoding="utf-8")
    else:
        repo_member.unlink()

    with pytest.raises(CheckpointProtocolError) as rejected:
        find_recovery_checkpoint(run_dir, expected_job_id="job_1")

    assert rejected.value.code in {
        "checkpoint_artifact_missing",
        "checkpoint_artifact_mismatch",
        "checkpoint_state_closure_invalid",
    }


def test_orphan_high_sequence_checkpoint_cannot_skip_required_stages(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    _artifact(run_dir, "output/eval_feedback.json", "{}")
    _write_completed_stage(
        run_dir,
        job_id="job_1",
        stage_name="evaluation",
        sequence=100,
        artifacts=["output/eval_feedback.json"],
        resume_from_stage="repair",
    )

    with pytest.raises(CheckpointProtocolError) as rejected:
        find_recovery_checkpoint(run_dir, expected_job_id="job_1")

    assert rejected.value.code == "checkpoint_stage_order_invalid"


def test_same_sequence_conflicting_stages_are_rejected(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    state = _write_chain(run_dir, through="evaluation")
    _write_completed_stage(
        run_dir,
        job_id="job_1",
        stage_name="repair",
        sequence=7,
        attempt=1,
        artifacts=state["evaluation"],
        resume_from_stage="evaluation",
    )
    _write_completed_stage(
        run_dir,
        job_id="job_1",
        stage_name="completed",
        sequence=7,
        attempt=2,
        artifacts=state["evaluation"],
        resume_from_stage=None,
    )

    with pytest.raises(CheckpointProtocolError) as rejected:
        list_checkpoints(run_dir, expected_job_id="job_1")

    assert rejected.value.code == "checkpoint_stage_order_invalid"


def test_stage_one_cannot_mix_mineru_branches(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _artifact(run_dir, "input/source_markdown.md", "# paper\n")
    for attempt, stage_name in enumerate(("mineru_parse", "mineru_skipped"), start=1):
        _write_completed_stage(
            run_dir,
            job_id="job_1",
            stage_name=stage_name,
            sequence=1,
            attempt=attempt,
            artifacts=["input/source_markdown.md"],
            resume_from_stage="planning",
        )

    with pytest.raises(CheckpointProtocolError) as rejected:
        list_checkpoints(run_dir, expected_job_id="job_1")

    assert rejected.value.code == "checkpoint_stage_order_invalid"


def test_resume_stage_must_be_the_legal_next_sequence(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    state = _state_files(run_dir)

    with pytest.raises(CheckpointProtocolError) as rejected:
        completed_checkpoint(
            run_dir,
            job_id="job_1",
            stage_name="planning",
            stage_sequence=2,
            stage_attempt=1,
            started_at="2026-07-22T00:00:00.000Z",
            completed_at="2026-07-22T00:00:01.000Z",
            artifact_paths=state["planning"],
            resume_from_stage="planning",
        )

    assert rejected.value.code == "checkpoint_stage_order_invalid"


@pytest.mark.parametrize("current_status", ["running", "failed"])
def test_incomplete_current_stage_resumes_previous_trusted_boundary_with_next_attempt(
    tmp_path: Path,
    current_status: str,
) -> None:
    run_dir = tmp_path / current_status
    _write_chain(run_dir, through="planning")
    if current_status == "running":
        current = running_checkpoint(
            job_id="job_1",
            stage_name="extract_config",
            stage_sequence=3,
            stage_attempt=1,
            started_at="2026-07-22T00:00:03.000Z",
        )
    else:
        current = failed_checkpoint(
            job_id="job_1",
            stage_name="extract_config",
            stage_sequence=3,
            stage_attempt=1,
            started_at="2026-07-22T00:00:03.000Z",
            completed_at="2026-07-22T00:00:04.000Z",
            error_code="stage_execution_failed",
        )
    write_checkpoint(run_dir, current)

    recovery = find_recovery_checkpoint(run_dir, expected_job_id="job_1")

    assert recovery is not None
    assert recovery.resume_from_stage == "extract_config"
    assert recovery.resume_sequence == 3
    assert recovery.resume_attempt == 2


def test_successful_resume_supersedes_stale_running_attempt(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    state = _write_chain(run_dir, through="coding")
    write_checkpoint(
        run_dir,
        running_checkpoint(
            job_id="job_1",
            stage_name="evaluation",
            stage_sequence=6,
            stage_attempt=1,
            started_at="2026-07-22T00:00:06.000Z",
        ),
    )
    _write_completed_stage(
        run_dir,
        job_id="job_1",
        stage_name="evaluation",
        sequence=6,
        attempt=2,
        artifacts=state["evaluation"],
        resume_from_stage="completed",
    )
    _artifact(run_dir, "run_status.json", '{"status":"completed"}')
    _artifact(run_dir, "run_summary.json", '{"status":"passed"}')
    _write_completed_stage(
        run_dir,
        job_id="job_1",
        stage_name="completed",
        sequence=7,
        artifacts=state["evaluation"] + ["run_status.json", "run_summary.json"],
        resume_from_stage=None,
    )

    entries = list_checkpoints(run_dir, expected_job_id="job_1")
    latest = latest_completed_checkpoint(run_dir, expected_job_id="job_1")

    assert len(entries) == 8
    assert latest is not None
    assert latest[0]["stage_name"] == "completed"
    assert latest[0]["stage_sequence"] == 7


def test_complete_trusted_checkpoint_chain_is_recoverable(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_chain(run_dir, through="evaluation")

    recovery = find_recovery_checkpoint(run_dir, expected_job_id="job_1")

    assert recovery is not None
    assert recovery.checkpoint["stage_name"] == "evaluation"
    assert recovery.resume_from_stage == "completed"
    assert recovery.resume_sequence == 7
    assert recovery.resume_attempt == 1
