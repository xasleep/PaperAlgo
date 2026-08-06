import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codes.checkpoint_protocol import (
    completed_checkpoint,
    failed_checkpoint,
    find_recovery_checkpoint,
    list_checkpoints,
    write_checkpoint,
)
from codes import run_pipeline
from codes.run_pipeline import PipelineCheckpointAdapter
from web_api.job_service import build_pipeline_command
from web_api.schemas import WebSettings


def _args(**overrides):
    values = {
        "checkpoint_mode": "sqlite",
        "resume_from_stage": "",
        "resume_stage_sequence": 0,
        "resume_stage_attempt": 0,
        "checkpoint_recovery_count": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _settings() -> WebSettings:
    return WebSettings(
        reproduce={"provider": "openai", "model": "fake", "api_key": "fake"},
        evaluation={
            "provider": "openai",
            "model": "fake",
            "api_key": "fake",
            "fallback_models": [],
        },
    )


def _input_boundary(run_dir: Path, job_id: str = "job_1") -> None:
    markdown = run_dir / "input" / "source_markdown.md"
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("# paper\n", encoding="utf-8")
    write_checkpoint(
        run_dir,
        completed_checkpoint(
            run_dir,
            job_id=job_id,
            stage_name="mineru_skipped",
            stage_sequence=1,
            stage_attempt=1,
            started_at="2026-07-22T00:00:00.000Z",
            completed_at="2026-07-22T00:00:01.000Z",
            artifact_paths=["input/source_markdown.md"],
            resume_from_stage="planning",
        ),
    )


def _failed_planning_attempt(run_dir: Path, job_id: str = "job_1") -> None:
    write_checkpoint(
        run_dir,
        failed_checkpoint(
            job_id=job_id,
            stage_name="planning",
            stage_sequence=2,
            stage_attempt=1,
            started_at="2026-07-22T00:00:02.000Z",
            completed_at="2026-07-22T00:00:03.000Z",
            error_code="stage_execution_failed",
        ),
    )


def test_adapter_writes_running_then_completed_and_skips_prior_stages(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    artifact = run_dir / "output" / "task_manifest.json"
    artifact.parent.mkdir(parents=True)
    _input_boundary(run_dir)
    _failed_planning_attempt(run_dir)
    adapter = PipelineCheckpointAdapter(
        _args(
            resume_from_stage="planning",
            resume_stage_sequence=2,
            resume_stage_attempt=2,
            checkpoint_recovery_count=1,
        ),
        run_dir,
        "job_1",
    )

    assert adapter.should_run(1) is False
    assert adapter.should_run(2) is True
    with adapter.stage(
        "planning",
        2,
        artifact_paths=lambda: ["output/task_manifest.json"],
        resume_from_stage="extract_config",
    ):
        running = list_checkpoints(run_dir, expected_job_id="job_1")
        current = [item for item in running if item[0]["stage_sequence"] == 2][-1]
        assert current[0]["status"] == "running"
        artifact.write_text('{"version":1,"files":["main.py"]}', encoding="utf-8")

    checkpoints = list_checkpoints(run_dir, expected_job_id="job_1")
    current = [item for item in checkpoints if item[0]["stage_sequence"] == 2][-1]
    assert current[0]["status"] == "completed"
    assert current[0]["stage_attempt"] == 2
    assert current[0]["resume_from_stage"] == "extract_config"


def test_adapter_records_failed_attempt_without_exception_details(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _input_boundary(run_dir)
    adapter = PipelineCheckpointAdapter(_args(), run_dir, "job_1")

    with pytest.raises(RuntimeError, match="sensitive-detail"):
        with adapter.stage(
            "planning",
            2,
            artifact_paths=lambda: [],
            resume_from_stage="extract_config",
        ):
            raise RuntimeError("sensitive-detail")

    checkpoint = [
        item
        for item in list_checkpoints(run_dir, expected_job_id="job_1")
        if item[0]["stage_sequence"] == 2
    ][0][0]
    assert checkpoint["status"] == "failed"
    assert checkpoint["error_code"] == "stage_execution_failed"
    assert "sensitive-detail" not in str(checkpoint)


def test_legacy_pipeline_command_is_unchanged_and_sqlite_flags_are_internal(
    tmp_path: Path,
) -> None:
    common = {
        "pdf_path": tmp_path / "paper.pdf",
        "job_id": "job_1",
        "paper_name": "paper",
        "settings": _settings(),
        "domain": "statistics",
        "eval_type": "ref_free",
        "generated_n": 1,
        "auto_refine": False,
        "max_repair_rounds": 0,
        "console_output": "quiet",
        "skip_mineru": False,
        "pdf_markdown_path": "",
    }

    legacy = build_pipeline_command(**common)
    sqlite = build_pipeline_command(
        **common,
        checkpoint_mode="sqlite",
        resume_from_stage="extract_config",
        resume_stage_sequence=3,
        resume_stage_attempt=2,
        checkpoint_recovery_count=1,
    )

    assert "--checkpoint_mode" not in legacy
    assert sqlite[-10:] == [
        "--checkpoint_mode",
        "sqlite",
        "--checkpoint_recovery_count",
        "1",
        "--resume_from_stage",
        "extract_config",
        "--resume_stage_sequence",
        "3",
        "--resume_stage_attempt",
        "2",
    ]


def test_fake_pipeline_resumes_at_previous_completed_boundary_without_rerunning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    source_dir = runs_dir / "source"
    source_dir.mkdir(parents=True)
    markdown = source_dir / "paper.md"
    markdown.write_text("# paper", encoding="utf-8")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    monkeypatch.setenv("REPRODUCE_API_KEY", "fake")
    monkeypatch.setenv("REPRODUCE_BASE_URL", "https://reproduce.invalid/v1")
    monkeypatch.setenv("EVAL_API_KEY", "fake")
    monkeypatch.setenv("EVAL_BASE_URL", "https://evaluation.invalid/v1")
    calls: list[str] = []
    fail_analyzing = {"value": True}

    def option(command: list[str], name: str) -> Path:
        return Path(command[command.index(name) + 1])

    def fake_run_command(label, command, *args, **kwargs):
        del args, kwargs
        calls.append(label)
        output_dir = option(command, "--output_dir")
        output_dir.mkdir(parents=True, exist_ok=True)
        if label == "planning":
            (output_dir / "task_manifest.json").write_text(
                '{"version":1,"files":[{"path":"main.py"}]}', encoding="utf-8"
            )
            (output_dir / "planning_response.json").write_text("{}", encoding="utf-8")
            (output_dir / "planning_trajectories.json").write_text("[]", encoding="utf-8")
        elif label == "extract_config":
            (output_dir / "planning_config.yaml").write_text("seed: 1\n", encoding="utf-8")
            artifacts = output_dir / "planning_artifacts"
            artifacts.mkdir(exist_ok=True)
            (artifacts / "1.1_overall_plan.txt").write_text("plan", encoding="utf-8")
        elif label == "analyzing":
            if fail_analyzing["value"]:
                fail_analyzing["value"] = False
                raise RuntimeError("simulated stage interruption")
            artifacts = output_dir / "analyzing_artifacts"
            artifacts.mkdir(exist_ok=True)
            (artifacts / "main_analysis.txt").write_text("analysis", encoding="utf-8")
            (output_dir / "main_simple_analysis_response.json").write_text(
                "{}", encoding="utf-8"
            )
        elif label == "coding":
            repo_dir = option(command, "--output_repo_dir")
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (output_dir / "repo_status.json").write_text(
                json.dumps({"status": "待测评", "repair_round": 0}),
                encoding="utf-8",
            )
        elif label == "evaluation":
            (output_dir / "eval_feedback.json").write_text("{}", encoding="utf-8")
            (output_dir / "repo_status.json").write_text(
                json.dumps({"status": "测评且通过", "repair_round": 0}),
                encoding="utf-8",
            )

    monkeypatch.setattr(run_pipeline, "run_command", fake_run_command)
    values = {
        "paper_pdf_path": str(pdf),
        "skip_mineru": True,
        "pdf_markdown_path": str(markdown),
        "paper_name": "paper",
        "domain": "statistics",
        "reproduce_provider": "openai",
        "reproduce_gpt_version": "gpt-4.1-mini",
        "eval_provider": "openai",
        "eval_gpt_version": "gpt-4.1-mini",
        "eval_fallback_gpt_versions": "",
        "runs_dir": str(runs_dir),
        "job_id": "resume_job",
        "checkpoint_mode": "sqlite",
        "resume_from_stage": "",
        "resume_stage_sequence": 0,
        "resume_stage_attempt": 0,
        "checkpoint_recovery_count": 0,
        "mineru_executable": "",
        "mineru_backend": "pipeline",
        "mineru_formula": True,
        "mineru_table": True,
        "data_dir": "../data",
        "eval_type": "ref_free",
        "generated_n": 1,
        "auto_refine": False,
        "max_repair_rounds": 0,
        "console_output": "quiet",
    }

    with pytest.raises(RuntimeError, match="simulated stage interruption"):
        run_pipeline.main(SimpleNamespace(**values))
    recovery = find_recovery_checkpoint(
        runs_dir / "resume_job", expected_job_id="resume_job"
    )
    assert recovery is not None
    assert recovery.resume_from_stage == "analyzing"
    assert recovery.resume_sequence == 4
    assert recovery.resume_attempt == 2

    calls.clear()
    values.update(
        resume_from_stage=recovery.resume_from_stage,
        resume_stage_sequence=recovery.resume_sequence,
        resume_stage_attempt=recovery.resume_attempt,
        checkpoint_recovery_count=1,
    )
    run_pipeline.main(SimpleNamespace(**values))

    assert calls == ["analyzing", "coding", "evaluation"]
    checkpoints = list_checkpoints(
        runs_dir / "resume_job", expected_job_id="resume_job"
    )
    sequences = [int(checkpoint["stage_sequence"]) for checkpoint, _ in checkpoints]
    assert sequences == sorted(sequences)
    assert checkpoints[-1][0]["stage_name"] == "completed"
    assert checkpoints[-1][0]["status"] == "completed"
