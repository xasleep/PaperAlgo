import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from web_api import main as main_module
from web_api.schemas import WebSettings


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"


def _load_code_module(module_name: str, file_name: str):
    codes_dir = Path(__file__).resolve().parents[1] / "codes"
    sys.path.insert(0, str(codes_dir))
    try:
        spec = importlib.util.spec_from_file_location(module_name, codes_dir / file_name)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(codes_dir))


def _arg_value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


def _web_settings() -> WebSettings:
    return WebSettings(
        reproduce={"provider": "openai", "model": "test-model", "api_key": "test-key"},
        evaluation={
            "provider": "openai",
            "model": "test-model",
            "api_key": "test-key",
        },
    )


def test_quota_like_error_falls_back_to_qwen_plus(monkeypatch) -> None:
    eval_module = _load_code_module("eval_for_fallback_test", "eval.py")
    calls = []

    class FakeQuotaError(Exception):
        status_code = 403

    def fake_make_client(model_name):
        calls.append(("client", model_name))
        return object()

    def fake_run_completion_requests(model_name, msg, generated_n):
        calls.append(("run", model_name))
        if model_name == "qwen3.7-max":
            raise FakeQuotaError("AllocationQuota.FreeTierOnly")
        return (
            {"model": model_name, "n": generated_n},
            {"model": model_name, "choices": [], "usage": {}},
            generated_n,
        )

    monkeypatch.setattr(eval_module, "PermissionDeniedError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "BadRequestError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "make_openai_client", fake_make_client)
    monkeypatch.setattr(
        eval_module,
        "run_completion_requests",
        fake_run_completion_requests,
    )

    request_json, completion_json, generated_n, actual_model, fallback_info = (
        eval_module.run_completion_requests_with_fallback(
            "qwen3.7-max",
            [{"role": "system", "content": "x"}],
            1,
            eval_module.default_fallback_models("qwen3.7-max"),
        )
    )

    assert actual_model == "qwen3.7-plus"
    assert request_json["model"] == "qwen3.7-plus"
    assert completion_json["model"] == "qwen3.7-plus"
    assert generated_n == 1
    assert fallback_info["fallback_used"] is True
    assert fallback_info["fallback_reason"] == "quota_like_error"
    assert fallback_info["fallback_from_model"] == "qwen3.7-max"
    assert fallback_info["fallback_eval_model"] == "qwen3.7-plus"
    assert fallback_info["fallback_remaining_models"] == []
    assert calls == [
        ("client", "qwen3.7-max"),
        ("run", "qwen3.7-max"),
        ("client", "qwen3.7-plus"),
        ("run", "qwen3.7-plus"),
    ]


def test_quota_like_error_keeps_remaining_fallback_chain(monkeypatch) -> None:
    eval_module = _load_code_module("eval_for_chain_fallback_test", "eval.py")
    calls = []

    class FakeQuotaError(Exception):
        status_code = 403

    def fake_make_client(model_name):
        calls.append(("client", model_name))
        return object()

    def fake_run_completion_requests(model_name, msg, generated_n):
        calls.append(("run", model_name))
        if model_name == "qwen3.7-max":
            raise FakeQuotaError("AllocationQuota.FreeTierOnly")
        return (
            {"model": model_name, "n": generated_n},
            {"model": model_name, "choices": [], "usage": {}},
            generated_n,
        )

    monkeypatch.setattr(eval_module, "PermissionDeniedError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "BadRequestError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "make_openai_client", fake_make_client)
    monkeypatch.setattr(
        eval_module,
        "run_completion_requests",
        fake_run_completion_requests,
    )

    _, _, _, actual_model, fallback_info = (
        eval_module.run_completion_requests_with_fallback(
            "qwen3.7-max",
            [{"role": "system", "content": "x"}],
            1,
            ["qwen3.7-plus", "gpt-4o-mini"],
        )
    )

    assert actual_model == "qwen3.7-plus"
    assert fallback_info["fallback_used"] is True
    assert fallback_info["fallback_eval_model"] == "qwen3.7-plus"
    assert fallback_info["fallback_model_chain"] == [
        "qwen3.7-max",
        "qwen3.7-plus",
        "gpt-4o-mini",
    ]
    assert fallback_info["fallback_remaining_models"] == ["gpt-4o-mini"]
    assert calls == [
        ("client", "qwen3.7-max"),
        ("run", "qwen3.7-max"),
        ("client", "qwen3.7-plus"),
        ("run", "qwen3.7-plus"),
    ]


def test_chained_quota_errors_fall_back_to_later_model(monkeypatch) -> None:
    eval_module = _load_code_module("eval_for_later_chain_fallback_test", "eval.py")
    calls = []

    class FakeQuotaError(Exception):
        status_code = 403

    def fake_make_client(model_name):
        calls.append(("client", model_name))
        return object()

    def fake_run_completion_requests(model_name, msg, generated_n):
        calls.append(("run", model_name))
        if model_name in {"qwen3.7-max", "qwen3.7-plus"}:
            raise FakeQuotaError("AllocationQuota.FreeTierOnly")
        return (
            {"model": model_name, "n": generated_n},
            {"model": model_name, "choices": [], "usage": {}},
            generated_n,
        )

    monkeypatch.setattr(eval_module, "PermissionDeniedError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "BadRequestError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "make_openai_client", fake_make_client)
    monkeypatch.setattr(
        eval_module,
        "run_completion_requests",
        fake_run_completion_requests,
    )

    request_json, _, _, actual_model, fallback_info = (
        eval_module.run_completion_requests_with_fallback(
            "qwen3.7-max",
            [{"role": "system", "content": "x"}],
            1,
            ["qwen3.7-plus", "gpt-4o-mini"],
        )
    )

    assert actual_model == "gpt-4o-mini"
    assert request_json["model"] == "gpt-4o-mini"
    assert fallback_info["fallback_used"] is True
    assert fallback_info["fallback_from_model"] == "qwen3.7-plus"
    assert fallback_info["fallback_eval_model"] == "gpt-4o-mini"
    assert fallback_info["fallback_remaining_models"] == []
    assert calls == [
        ("client", "qwen3.7-max"),
        ("run", "qwen3.7-max"),
        ("client", "qwen3.7-plus"),
        ("run", "qwen3.7-plus"),
        ("client", "gpt-4o-mini"),
        ("run", "gpt-4o-mini"),
    ]


def test_eval_main_records_actual_fallback_model(monkeypatch, tmp_path: Path) -> None:
    eval_module = _load_code_module("eval_for_record_test", "eval.py")
    data_dir = tmp_path / "data"
    prompts_dir = data_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "ref_free.txt").write_text("{{Paper}}\n{{Code}}\n", encoding="utf-8")
    markdown_path = tmp_path / "paper.md"
    markdown_path.write_text("# paper\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    target_repo_dir = tmp_path / "repo"
    eval_result_dir = tmp_path / "results"
    output_dir.mkdir()
    target_repo_dir.mkdir()
    (target_repo_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")

    fallback_info = {
        "fallback_used": True,
        "fallback_reason": "quota_like_error",
        "fallback_from_model": "qwen3.7-max",
        "fallback_eval_model": "qwen3.7-plus",
        "fallback_model_chain": ["qwen3.7-max", "qwen3.7-plus"],
        "fallback_remaining_models": [],
    }
    completion_json = {
        "model": "qwen3.7-plus",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "score": 3,
                            "critique_list": [
                                {
                                    "file_name": "main.py",
                                    "severity_level": "medium",
                                    "critique": "needs work",
                                }
                            ],
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    monkeypatch.setattr(eval_module, "num_tokens_from_messages", lambda msg: 1)
    monkeypatch.setattr(eval_module, "print_log_cost", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        eval_module,
        "run_completion_requests_with_fallback",
        lambda *args, **kwargs: (
            {"model": "qwen3.7-plus", "n": 1},
            completion_json,
            1,
            "qwen3.7-plus",
            fallback_info,
        ),
    )

    args = SimpleNamespace(
        paper_name="paper",
        paper_format="Markdown",
        domain="general",
        pdf_json_path=None,
        pdf_latex_path=None,
        pdf_markdown_path=str(markdown_path),
        data_dir=str(data_dir),
        output_dir=str(output_dir),
        target_repo_dir=str(target_repo_dir),
        gold_repo_dir="",
        eval_result_dir=str(eval_result_dir),
        eval_type="ref_free",
        generated_n=1,
        gpt_version="qwen3.7-max",
        fallback_gpt_versions="",
        selected_file_path="",
        papercoder=False,
        max_repair_rounds=0,
    )

    eval_module.main(args)

    repo_status = eval_module.load_json_file(eval_module.repo_status_path(str(output_dir)))
    feedback = eval_module.load_json_file(eval_module.eval_feedback_path(str(output_dir)))
    assert repo_status["requested_eval_model"] == "qwen3.7-max"
    assert repo_status["eval_model"] == "qwen3.7-plus"
    assert repo_status["fallback_used"] is True
    assert repo_status["fallback_reason"] == "quota_like_error"
    assert repo_status["fallback_eval_model"] == "qwen3.7-plus"
    assert repo_status["fallback_remaining_models"] == []
    assert repo_status["max_repair_rounds"] == 0
    assert feedback["eval_model"] == "qwen3.7-plus"


def test_auto_refine_reuses_fallback_model_after_first_quota_failure(tmp_path: Path) -> None:
    run_pipeline = _load_code_module("run_pipeline_for_fallback_test", "run_pipeline.py")
    args = SimpleNamespace(
        domain="statistics",
        data_dir="data",
        eval_type="ref_free",
        generated_n=1,
        eval_gpt_version="qwen3.7-max",
        eval_fallback_gpt_versions="qwen3.7-plus",
        max_repair_rounds=2,
    )

    first_cmd = run_pipeline.build_eval_cmd(
        args,
        "codes",
        "paper",
        "paper.md",
        "output",
        "repo",
        "results",
    )
    assert _arg_value(first_cmd, "--gpt_version") == "qwen3.7-max"
    assert _arg_value(first_cmd, "--fallback_gpt_versions") == "qwen3.7-plus"

    status_path = tmp_path / "run_status.json"
    changed = run_pipeline.remember_fallback_eval_model(
        args,
        {
            "fallback_used": True,
            "fallback_reason": "quota_like_error",
            "fallback_eval_model": "qwen3.7-plus",
            "eval_model": "qwen3.7-plus",
        },
        str(status_path),
    )
    assert changed is True

    next_cmd = run_pipeline.build_eval_cmd(
        args,
        "codes",
        "paper",
        "paper.md",
        "output",
        "repo",
        "results",
    )
    assert _arg_value(next_cmd, "--gpt_version") == "qwen3.7-plus"
    assert "--fallback_gpt_versions" not in next_cmd
    status = run_pipeline.load_json_file(str(status_path))
    assert status["requested_eval_model"] == "qwen3.7-max"
    assert status["eval_fallback_active"] is True
    assert status["effective_eval_model"] == "qwen3.7-plus"
    assert status["remaining_eval_fallback_models"] == []


def test_auto_refine_keeps_remaining_fallback_chain_after_first_fallback(
    tmp_path: Path,
) -> None:
    run_pipeline = _load_code_module(
        "run_pipeline_for_multi_fallback_test",
        "run_pipeline.py",
    )
    args = SimpleNamespace(
        domain="statistics",
        data_dir="data",
        eval_type="ref_free",
        generated_n=1,
        eval_gpt_version="qwen3.7-max",
        eval_fallback_gpt_versions="qwen3.7-plus,gpt-4o-mini",
        max_repair_rounds=2,
    )

    first_cmd = run_pipeline.build_eval_cmd(
        args,
        "codes",
        "paper",
        "paper.md",
        "output",
        "repo",
        "results",
    )
    assert _arg_value(first_cmd, "--gpt_version") == "qwen3.7-max"
    assert (
        _arg_value(first_cmd, "--fallback_gpt_versions")
        == "qwen3.7-plus,gpt-4o-mini"
    )

    status_path = tmp_path / "run_status.json"
    changed = run_pipeline.remember_fallback_eval_model(
        args,
        {
            "fallback_used": True,
            "fallback_reason": "quota_like_error",
            "fallback_from_model": "qwen3.7-max",
            "fallback_eval_model": "qwen3.7-plus",
            "eval_model": "qwen3.7-plus",
            "fallback_model_chain": [
                "qwen3.7-max",
                "qwen3.7-plus",
                "gpt-4o-mini",
            ],
            "fallback_remaining_models": ["gpt-4o-mini"],
        },
        str(status_path),
    )
    assert changed is True

    next_cmd = run_pipeline.build_eval_cmd(
        args,
        "codes",
        "paper",
        "paper.md",
        "output",
        "repo",
        "results",
    )
    assert _arg_value(next_cmd, "--gpt_version") == "qwen3.7-plus"
    assert _arg_value(next_cmd, "--fallback_gpt_versions") == "gpt-4o-mini"
    status = run_pipeline.load_json_file(str(status_path))
    assert status["requested_eval_model"] == "qwen3.7-max"
    assert status["effective_eval_model"] == "qwen3.7-plus"
    assert status["fallback_eval_model"] == "qwen3.7-plus"
    assert status["fallback_from_model"] == "qwen3.7-max"
    assert status["fallback_reason"] == "quota_like_error"
    assert status["eval_fallback_active"] is True
    assert status["remaining_eval_fallback_models"] == ["gpt-4o-mini"]
    assert status["eval_fallback_model_chain"] == [
        "qwen3.7-max",
        "qwen3.7-plus",
        "gpt-4o-mini",
    ]


def test_auto_refine_keeps_original_model_and_fallbacks_when_no_fallback_used(
    tmp_path: Path,
) -> None:
    run_pipeline = _load_code_module(
        "run_pipeline_for_no_fallback_test",
        "run_pipeline.py",
    )
    args = SimpleNamespace(
        domain="statistics",
        data_dir="data",
        eval_type="ref_free",
        generated_n=1,
        eval_gpt_version="qwen3.7-max",
        eval_fallback_gpt_versions="qwen3.7-plus,gpt-4o-mini",
        max_repair_rounds=2,
    )

    status_path = tmp_path / "run_status.json"
    changed = run_pipeline.remember_fallback_eval_model(
        args,
        {
            "fallback_used": False,
            "eval_model": "qwen3.7-max",
            "fallback_model_chain": [
                "qwen3.7-max",
                "qwen3.7-plus",
                "gpt-4o-mini",
            ],
        },
        str(status_path),
    )
    assert changed is False

    next_cmd = run_pipeline.build_eval_cmd(
        args,
        "codes",
        "paper",
        "paper.md",
        "output",
        "repo",
        "results",
    )
    assert _arg_value(next_cmd, "--gpt_version") == "qwen3.7-max"
    assert (
        _arg_value(next_cmd, "--fallback_gpt_versions")
        == "qwen3.7-plus,gpt-4o-mini"
    )
    assert not status_path.exists()


@pytest.mark.parametrize("generated_n", [0, 33])
def test_generated_n_boundaries_are_enforced(generated_n: int) -> None:
    run_pipeline = _load_code_module("run_pipeline_for_generated_bounds", "run_pipeline.py")
    args = SimpleNamespace(generated_n=generated_n, max_repair_rounds=3)

    with pytest.raises(ValueError, match="generated_n"):
        run_pipeline.validate_runtime_args(args)


@pytest.mark.parametrize("max_repair_rounds", [-1, 11])
def test_max_repair_round_boundaries_are_enforced(max_repair_rounds: int) -> None:
    eval_module = _load_code_module("eval_for_repair_bounds", "eval.py")
    args = SimpleNamespace(generated_n=1, max_repair_rounds=max_repair_rounds)

    with pytest.raises(ValueError, match="max_repair_rounds"):
        eval_module.validate_eval_args(args)


def test_zero_repair_rounds_is_evaluate_only_policy() -> None:
    run_pipeline = _load_code_module("run_pipeline_for_zero_repair", "run_pipeline.py")
    args = SimpleNamespace(generated_n=1, max_repair_rounds=0)

    run_pipeline.validate_runtime_args(args)
    assert (
        run_pipeline.repair_limit_message(0, 0)
        == "Evaluation failed and max_repair_rounds=0, so no repair was attempted."
    )


@pytest.mark.parametrize(
    "form_override, expected_detail",
    [
        ({"generated_n": "33"}, "generated_n must be between 1 and 32."),
        ({"max_repair_rounds": "11"}, "max_repair_rounds must be between 0 and 10."),
    ],
)
def test_web_create_job_rejects_out_of_range_parameters(
    monkeypatch,
    form_override: dict[str, str],
    expected_detail: str,
) -> None:
    monkeypatch.setattr(main_module, "load_settings", _web_settings)
    client = TestClient(main_module.app, base_url=LOCAL_ORIGIN)
    session = client.get(f"{API_PREFIX}/session", headers={"Origin": LOCAL_ORIGIN})
    client.headers.update(
        {"Origin": LOCAL_ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]}
    )
    payload = {
        "upload_id": "0" * 32,
        "paper_name": "paper",
        "domain": "statistics",
        "eval_type": "ref_free",
        "generated_n": "1",
        "auto_refine": "false",
        "max_repair_rounds": "0",
        "console_output": "quiet",
        "skip_mineru": "false",
        "pdf_markdown_path": "",
        **form_override,
    }

    response = client.post(f"{API_PREFIX}/jobs", json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_parameter"
    assert response.json()["error"]["message"] == expected_detail
