import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from codes.provider_registry import ProviderRegistry
from web_api import database as database_module
from web_api.database import connect_database, initialize_database
from web_api.errors import InvalidStateTransitionError
from web_api.job_repository import JobRepository


CODES_DIR = Path(__file__).resolve().parents[1] / "codes"
sys.path.insert(0, str(CODES_DIR))

from task_manifest import (  # noqa: E402
    InvalidTaskPathError,
    TaskManifestError,
    UnsafeTaskWriteError,
    parse_task_manifest,
)


def _load_code_module(module_name: str, file_name: str):
    spec = importlib.util.spec_from_file_location(module_name, CODES_DIR / file_name)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quality_result(**overrides: object) -> dict[str, object]:
    from codes.evaluation_contract import build_quality_result

    payload: dict[str, object] = {
        "paper_name": "paper",
        "target_repo_dir": "repo",
        "eval_type": "ref_free",
        "requested_eval_model": "fake-primary",
        "eval_model": "fake-primary",
        "provider_id": "fake",
        "generated_n": 3,
        "scores": [5, 3, 4],
        "findings": [],
        "files_to_fix": [],
        "repair_round": 0,
        "max_repair_rounds": 2,
    }
    payload.update(overrides)
    return build_quality_result(**payload)


def _request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
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
    request.update(overrides)
    return request


def _synthetic_model(
    *,
    fallback_model_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "base_url": None,
        "base_url_env": "FAKE_BASE_URL",
        "api_key_env": "FAKE_API_KEY",
        "max_n": 1,
        "context_window": None,
        "max_output_tokens": None,
        "json_schema_support": None,
        "usage_support": True,
        "cache_token_support": True,
        "timeout_seconds": 2,
        "max_retries": 0,
        "max_concurrency": 2,
        "pricing": {
            "status": "unknown",
            "currency": None,
            "input_per_million": None,
            "cached_input_per_million": None,
            "output_per_million": None,
            "effective_date": None,
        },
        "request_options": {},
        "fallback_model_ids": fallback_model_ids or [],
    }


def _synthetic_registry() -> ProviderRegistry:
    return ProviderRegistry.from_mapping(
        {
            "version": 1,
            "providers": {
                "fake": {
                    "models": {
                        "fake-primary": _synthetic_model(
                            fallback_model_ids=[
                                "fake-fallback-a",
                                "fake-fallback-b",
                            ],
                        ),
                        "fake-fallback-a": _synthetic_model(),
                        "fake-fallback-b": _synthetic_model(),
                    }
                }
            },
        }
    )


def test_quality_result_schema_accepts_explicit_completed_quality_failure() -> None:
    from codes.evaluation_contract import (
        EVALUATION_RESULT_SCHEMA_VERSION,
        decide_repair_action,
        validate_evaluation_result,
    )

    result = _quality_result(
        scores=[3, 4, 4],
        findings=[
            {
                "file_name": "main.py",
                "severity_level": "medium",
                "critique": "missing a required experiment",
            }
        ],
        files_to_fix=["main.py"],
    )

    assert validate_evaluation_result(result) == result
    assert result["schema_version"] == EVALUATION_RESULT_SCHEMA_VERSION
    assert result["execution_status"] == "completed"
    assert result["evaluation_status"] == "completed"
    assert result["quality_status"] == "rejected"
    assert result["quality_verdict"] == "failed"
    assert result["quality_score"] == pytest.approx(11 / 3)
    assert result["repair_status"] == "pending"
    assert result["files_to_fix"] == ["main.py"]
    assert decide_repair_action(result)["status"] == "ready"


@pytest.mark.parametrize(
    "mutations",
    [
        {"evaluation_status": "pending"},
        {"evaluation_status": "skipped"},
        {
            "quality_status": "rejected",
            "quality_verdict": "passed",
            "repair_status": "not_applicable",
        },
        {
            "quality_status": "accepted",
            "quality_verdict": "failed",
            "repair_status": "pending",
        },
        {
            "evaluation_status": "failed",
            "quality_status": "accepted",
            "quality_verdict": "passed",
            "repair_status": "not_applicable",
            "errors": [{"code": "evaluator_unavailable", "message": "sanitized"}],
        },
    ],
)
def test_contract_rejects_illegal_status_quality_combinations(
    mutations: dict[str, object],
) -> None:
    from codes.evaluation_contract import EvaluationContractError, validate_evaluation_result

    result = _quality_result(
        scores=[2, 3, 3],
        findings=[
            {
                "file_name": "main.py",
                "severity_level": "medium",
                "critique": "missing a required experiment",
            }
        ],
        files_to_fix=["main.py"],
    )
    result.update(mutations)

    with pytest.raises(EvaluationContractError):
        validate_evaluation_result(result)


def test_repair_action_ready_requires_pending_repair_status() -> None:
    from codes.evaluation_contract import decide_repair_action

    result = _quality_result(
        scores=[2, 3, 3],
        findings=[
            {
                "file_name": "main.py",
                "severity_level": "medium",
                "critique": "missing a required experiment",
            }
        ],
        files_to_fix=["main.py"],
    )
    result["repair_status"] = "blocked"

    assert decide_repair_action(result) == {
        "status": "blocked",
        "reason": "repair_status_not_pending",
        "attempt": 0,
        "max_attempts": 2,
        "files_to_fix": ["main.py"],
    }


def test_schema_rejects_unknown_fields_raw_payloads_and_leaky_errors() -> None:
    from codes.evaluation_contract import (
        EvaluationContractError,
        build_evaluation_error_result,
        validate_evaluation_result,
    )

    valid = _quality_result()
    for forbidden_key in ("request_json", "completion_json", "prompt", "model_response"):
        with pytest.raises(EvaluationContractError):
            validate_evaluation_result({**valid, forbidden_key: {"secret": "x"}})

    with pytest.raises(EvaluationContractError):
        validate_evaluation_result({**valid, "unknown": True})

    with pytest.raises(EvaluationContractError):
        build_evaluation_error_result(
            paper_name="paper",
            target_repo_dir="repo",
            eval_type="ref_free",
            requested_eval_model="fake-primary",
            provider_id="fake",
            error_code="provider_protocol_error",
            error_message="raw prompt and sk-SECRET should not persist",
            generated_n=1,
            repair_round=0,
            max_repair_rounds=1,
        )


@pytest.mark.parametrize(
    "error_code",
    [
        "evaluator_timeout",
        "provider_protocol_error",
        "evaluator_unavailable",
        "malformed_evaluator_response",
    ],
)
def test_evaluation_protocol_failures_do_not_become_quality_rejections(
    error_code: str,
) -> None:
    from codes.evaluation_contract import (
        build_evaluation_error_result,
        decide_repair_action,
        validate_evaluation_result,
    )

    result = build_evaluation_error_result(
        paper_name="paper",
        target_repo_dir="repo",
        eval_type="ref_free",
        requested_eval_model="fake-primary",
        provider_id="fake",
        error_code=error_code,
        error_message="sanitized stable message",
        generated_n=2,
        repair_round=0,
        max_repair_rounds=1,
    )

    validate_evaluation_result(result)
    assert result["execution_status"] == "completed"
    assert result["evaluation_status"] == "failed"
    assert result["quality_status"] == "skipped"
    assert result["quality_verdict"] == "not_assessed"
    assert result["quality_score"] is None
    assert result["repair_status"] == "blocked"
    assert decide_repair_action(result)["status"] == "blocked"
    assert decide_repair_action(result)["reason"] == error_code


def test_execution_failure_marks_evaluation_not_run_and_blocks_repair() -> None:
    from codes.evaluation_contract import (
        build_execution_failed_result,
        decide_repair_action,
        validate_evaluation_result,
    )

    result = build_execution_failed_result(
        paper_name="paper",
        target_repo_dir="repo",
        eval_type="ref_free",
        failure_code="pipeline_process_failed",
        repair_round=0,
        max_repair_rounds=2,
    )

    validate_evaluation_result(result)
    assert result["execution_status"] == "failed"
    assert result["evaluation_status"] == "skipped"
    assert result["quality_status"] == "skipped"
    assert decide_repair_action(result) == {
        "status": "blocked",
        "reason": "execution_failed",
        "attempt": 0,
        "max_attempts": 2,
        "files_to_fix": [],
    }


def test_quorum_controls_quality_verdict_and_repair_eligibility() -> None:
    from codes.evaluation_contract import (
        build_quorum_not_met_result,
        decide_repair_action,
        has_quorum,
        required_quorum,
        validate_evaluation_result,
    )

    assert required_quorum(5) == 3
    assert has_quorum(valid_n=3, generated_n=5) is True
    assert has_quorum(valid_n=2, generated_n=5) is False

    result = build_quorum_not_met_result(
        paper_name="paper",
        target_repo_dir="repo",
        eval_type="ref_free",
        requested_eval_model="fake-primary",
        eval_model="fake-primary",
        provider_id="fake",
        generated_n=5,
        valid_n=2,
        repair_round=0,
        max_repair_rounds=1,
    )

    validate_evaluation_result(result)
    assert result["evaluation_status"] == "failed"
    assert result["quality_verdict"] == "not_assessed"
    assert result["repair_status"] == "blocked"
    assert decide_repair_action(result)["reason"] == "quorum_not_met"


def test_files_to_fix_empty_means_no_repair_and_no_manifest_wide_fallback() -> None:
    from codes.evaluation_contract import decide_repair_action, resolve_files_to_fix

    manifest = parse_task_manifest(["main.py", "helpers.py", "config.yaml"])
    result = _quality_result(
        scores=[2, 3, 3],
        files_to_fix=[],
        findings=[
            {
                "file_name": "repository",
                "severity_level": "medium",
                "critique": "overall issue without a safe file target",
            }
        ],
    )

    assert resolve_files_to_fix([], manifest) == ()
    assert decide_repair_action(result) == {
        "status": "skipped",
        "reason": "no_files_to_fix",
        "attempt": 0,
        "max_attempts": 2,
        "files_to_fix": [],
    }

    source = (CODES_DIR / "3_coding.py").read_text(encoding="utf-8")
    assert "if not repair_task_files:" not in source
    assert "for task in task_manifest.files" not in source.split(
        "if repair_from_eval:", 1
    )[1].split("repair_files =", 1)[0]


@pytest.mark.parametrize(
    "raw_paths",
    [
        ["../../outside.py"],
        ["C:/outside.py"],
        ["/tmp/outside.py"],
        ["helpers.py"],
    ],
)
def test_files_to_fix_must_be_manifest_members_with_safe_paths(raw_paths: list[str]) -> None:
    from codes.evaluation_contract import resolve_files_to_fix

    manifest = parse_task_manifest(["main.py"])

    with pytest.raises((InvalidTaskPathError, TaskManifestError)):
        resolve_files_to_fix(raw_paths, manifest)


def test_files_to_fix_rejects_symlink_target_when_checked_against_repo(
    tmp_path: Path,
) -> None:
    from codes.evaluation_contract import resolve_files_to_fix

    repo_root = tmp_path / "repo"
    outside = tmp_path / "outside.py"
    repo_root.mkdir()
    outside.write_text("outside", encoding="utf-8")
    target = repo_root / "main.py"
    try:
        target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is not permitted on this host: {exc}")

    manifest = parse_task_manifest(["main.py"])

    with pytest.raises(UnsafeTaskWriteError):
        resolve_files_to_fix(["main.py"], manifest, repo_root=repo_root)


def test_repair_attempt_limit_and_completed_attempt_idempotency() -> None:
    from codes.evaluation_contract import decide_repair_action

    result = _quality_result(
        scores=[2, 3, 3],
        files_to_fix=["main.py"],
        repair_round=2,
        max_repair_rounds=2,
    )
    assert decide_repair_action(result) == {
        "status": "blocked",
        "reason": "repair_limit_reached",
        "attempt": 2,
        "max_attempts": 2,
        "files_to_fix": ["main.py"],
    }

    retry = _quality_result(
        scores=[2, 3, 3],
        files_to_fix=["main.py"],
        repair_round=1,
        max_repair_rounds=2,
        completed_repair_attempts=[
            {
                "attempt": 2,
                "files_to_fix": ["main.py"],
                "status": "completed",
                "result": "pending_evaluation",
            }
        ],
    )
    assert decide_repair_action(retry) == {
        "status": "skipped",
        "reason": "repair_already_completed",
        "attempt": 2,
        "max_attempts": 2,
        "files_to_fix": ["main.py"],
    }


def test_eval_fallback_exhaustion_is_deterministic_and_sanitized(monkeypatch) -> None:
    from codes.evaluation_contract import classify_evaluation_exception

    eval_module = _load_code_module("eval_for_pr06a_fallback_exhaustion", "eval.py")
    fallback_calls: list[tuple[str, str, str]] = []

    class FakeQuotaError(Exception):
        status_code = 403

    registry = _synthetic_registry()

    def fake_make_client(provider_id: str, model_name: str) -> object:
        fallback_calls.append(("client", provider_id, model_name))
        return object()

    def fake_run_completion_requests(
        provider_id: str,
        model_name: str,
        msg: list[dict[str, str]],
        generated_n: int,
        input_tokens: int | None = None,
    ) -> tuple[dict[str, object], dict[str, object], int]:
        fallback_calls.append(("run", provider_id, model_name))
        raise FakeQuotaError(f"AllocationQuota.FreeTierOnly sk-SECRET {model_name}")

    monkeypatch.setattr(eval_module, "PermissionDeniedError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "BadRequestError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(eval_module, "make_openai_client", fake_make_client)
    monkeypatch.setattr(eval_module, "run_completion_requests", fake_run_completion_requests)

    with pytest.raises(FakeQuotaError) as exc_info:
        eval_module.run_completion_requests_with_fallback(
            "fake",
            "fake-primary",
            [{"role": "system", "content": "prompt must not persist"}],
            1,
            ["fake-fallback-a", "fake-fallback-b"],
        )

    assert fallback_calls == [
        ("client", "fake", "fake-primary"),
        ("run", "fake", "fake-primary"),
        ("client", "fake", "fake-fallback-a"),
        ("run", "fake", "fake-fallback-a"),
        ("client", "fake", "fake-fallback-b"),
        ("run", "fake", "fake-fallback-b"),
    ]
    classified = classify_evaluation_exception(exc_info.value)
    assert classified["error_code"] == "evaluator_unavailable"
    assert "sk-SECRET" not in classified["message"]
    assert "prompt must not persist" not in json.dumps(classified)


def _run_eval_entry_with_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_payload: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    eval_module = _load_code_module(
        f"eval_malformed_entry_{abs(hash(json.dumps(response_payload, sort_keys=True)))}",
        "eval.py",
    )
    data_dir = tmp_path / "data"
    prompts_dir = data_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "ref_free.txt").write_text(
        "Evaluate {{Paper}} against {{Code}}.",
        encoding="utf-8",
    )
    markdown = tmp_path / "paper.md"
    markdown.write_text("# paper\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    target_repo_dir = tmp_path / "repo"
    eval_result_dir = tmp_path / "results"
    target_repo_dir.mkdir()
    (target_repo_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(eval_module, "num_tokens_from_messages", lambda messages: 1)

    def fake_completion_with_fallback(*args: object, **kwargs: object):
        del args, kwargs
        completion_json = {
            "model": "fake-primary",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(response_payload, ensure_ascii=False),
                    },
                }
            ],
            "usage": None,
        }
        return {}, completion_json, 1, "fake-primary", {}

    monkeypatch.setattr(
        eval_module,
        "run_completion_requests_with_fallback",
        fake_completion_with_fallback,
    )

    args = SimpleNamespace(
        paper_name="paper",
        paper_format="Markdown",
        domain="statistics",
        pdf_json_path=None,
        pdf_latex_path=None,
        pdf_markdown_path=str(markdown),
        output_dir=str(output_dir),
        target_repo_dir=str(target_repo_dir),
        eval_result_dir=str(eval_result_dir),
        gpt_version="fake-primary",
        provider="fake",
        fallback_gpt_versions=[],
        generated_n=1,
        max_repair_rounds=1,
        data_dir=str(data_dir),
        eval_type="ref_free",
        papercoder=False,
        gold_repo_dir="",
        selected_file_path="",
    )

    monkeypatch.setattr(eval_module, "default_fallback_models", lambda *args: [])
    eval_module.main(args)

    feedback = json.loads((output_dir / "eval_feedback.json").read_text(encoding="utf-8"))
    status = json.loads((output_dir / "repo_status.json").read_text(encoding="utf-8"))
    result_path = Path(str(feedback["eval_result_file"]))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    return feedback, status, result


@pytest.mark.parametrize(
    "response_payload",
    [
        {"score": 2, "critique_list": ["bad UNIQUE-EVAL-SECRET"]},
        {"score": 2},
        {
            "score": 2,
            "critique_list": [
                {
                    "file_name": "main.py",
                    "severity_level": ["high"],
                    "critique": "bad",
                }
            ],
        },
        {
            "score": 2,
            "critique_list": [
                {
                    "file_name": "main.py",
                    "severity_level": "high",
                    "critique": "bad",
                },
                "mixed",
            ],
        },
    ],
)
def test_malformed_evaluator_feedback_is_persisted_as_structured_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_payload: dict[str, object],
) -> None:
    feedback, status, result = _run_eval_entry_with_response(
        tmp_path,
        monkeypatch,
        response_payload,
    )

    for payload in (feedback, status, result):
        assert payload["evaluation_status"] == "failed"
        assert payload["quality_status"] == "skipped"
        assert payload["quality_verdict"] == "not_assessed"
        assert payload["repair_status"] == "blocked"
        assert payload["errors"][0]["code"] == "malformed_evaluator_response"
        assert payload["files_to_fix"] == []
    assert feedback["files_to_repair"] == []
    assert feedback["passed"] is False
    serialized = json.dumps([feedback, status, result], ensure_ascii=False)
    assert "UNIQUE-EVAL-SECRET" not in serialized
    assert "Evaluate {{Paper}}" not in serialized


def test_sqlite_state_machine_separates_evaluation_and_quality_statuses(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(job_id="job_state", request=_request(), paper_name="paper")
    repository.transition_job("job_state", expected_version=1, execution_status="running")
    completed = repository.transition_job(
        "job_state",
        expected_version=2,
        execution_status="completed",
        evaluation_status="completed",
    )

    assert completed["execution_status"] == "completed"
    assert completed["evaluation_status"] == "completed"
    assert completed["quality_status"] == "pending"

    rejected = repository.transition_job(
        "job_state",
        expected_version=3,
        quality_status="rejected",
    )
    assert rejected["quality_status"] == "rejected"

    with pytest.raises(InvalidStateTransitionError):
        repository.transition_job(
            "job_state",
            expected_version=4,
            evaluation_status="passed",
        )


def test_execution_failure_skips_evaluation_in_sqlite_state_machine(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "paper2code.db")
    repository.create_job(job_id="job_failed", request=_request(), paper_name="paper")
    failed = repository.transition_job(
        "job_failed",
        expected_version=1,
        execution_status="failed",
        evaluation_status="skipped",
        quality_status="skipped",
    )

    assert failed["execution_status"] == "failed"
    assert failed["evaluation_status"] == "skipped"
    assert failed["quality_status"] == "skipped"


def test_migration_7_preserves_legacy_jobs_and_adds_repair_attempts_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "version-6" / "paper2code.db"
    current_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations[:6])
    initialize_database(db_path)
    with closing(connect_database(db_path)) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, request_hash, paper_name, upload_id, domain, eval_type,
                    generated_n, auto_refine, max_repair_rounds, console_output,
                    skip_mineru, pdf_markdown_path, execution_status,
                    evaluation_status, quality_status, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy_quality_failed",
                    "0" * 64,
                    "paper",
                    "0" * 32,
                    "statistics",
                    "ref_free",
                    1,
                    1,
                    2,
                    "quiet",
                    0,
                    "",
                    "completed",
                    "failed",
                    "pending",
                    3,
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                ),
            )

    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations)
    initialize_database(db_path)
    initialize_database(db_path)

    with closing(connect_database(db_path)) as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        legacy = connection.execute(
            "SELECT execution_status, evaluation_status, quality_status "
            "FROM jobs WHERE job_id = 'legacy_quality_failed'"
        ).fetchone()
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert versions == [1, 2, 3, 4, 5, 6, 7]
    assert tuple(legacy) == ("completed", "completed", "rejected")
    assert "repair_attempts" in tables
