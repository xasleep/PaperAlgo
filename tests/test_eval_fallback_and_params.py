import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from codes.provider_registry import DEFAULT_REGISTRY_PATH, ProviderRegistry
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
        reproduce={
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "api_key": "test-key",
            "base_url": "https://reproduce.invalid/v1",
        },
        evaluation={
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "api_key": "test-key",
            "base_url": "https://evaluation.invalid/v1",
        },
    )


class _DumpableCompletion:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def model_dump_json(self) -> str:
        return json.dumps(self.payload)


class _FakeCompletionDelegate:
    def __init__(self, payloads: list[dict[str, object]]):
        self.calls: list[dict[str, object]] = []
        self._payloads = list(payloads)

    def create(self, **kwargs: object) -> _DumpableCompletion:
        self.calls.append(kwargs)
        if not self._payloads:
            raise AssertionError("unexpected delegate.create call")
        return _DumpableCompletion(self._payloads.pop(0))


class _FakeChatClient:
    def __init__(self, delegate: _FakeCompletionDelegate):
        self.chat = SimpleNamespace(completions=delegate)


def _completion_payload(content: str, *, usage_marker: object = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "choices": [{"message": {"content": content}}],
    }
    if usage_marker != "__missing__":
        payload["usage"] = usage_marker
    return payload


def _synthetic_model(
    *,
    max_n: int = 1,
    request_options: dict[str, object] | None = None,
    fallback_model_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "base_url": None,
        "base_url_env": "FAKE_BASE_URL",
        "api_key_env": "FAKE_API_KEY",
        "max_n": max_n,
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
        "request_options": request_options or {},
        "fallback_model_ids": fallback_model_ids or [],
    }


def _synthetic_registry(
    *,
    provider_id: str = "fake",
    primary_model_id: str = "fake-primary",
    fallback_model_ids: tuple[str, ...] = (
        "fake-fallback-a",
        "fake-fallback-b",
        "fake-fallback-c",
    ),
    max_n: int = 1,
    request_options: dict[str, object] | None = None,
) -> ProviderRegistry:
    models = {
        primary_model_id: _synthetic_model(
            max_n=max_n,
            request_options=request_options,
            fallback_model_ids=list(fallback_model_ids),
        )
    }
    for model_id in fallback_model_ids:
        models[model_id] = _synthetic_model(max_n=max_n, request_options=request_options)
    return ProviderRegistry.from_mapping(
        {
            "version": 1,
            "providers": {
                provider_id: {"models": models},
                "other": {
                    "models": {
                        "other-fallback": _synthetic_model(),
                    }
                },
            },
        }
    )


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("", []),
        ([], []),
        ("fake-b,fake-c", ["fake-b", "fake-c"]),
        (",", ["", ""]),
        (",fake-b", ["", "fake-b"]),
        ("fake-b,", ["fake-b", ""]),
        ("fake-b,,fake-c", ["fake-b", "", "fake-c"]),
        (" fake-b", [" fake-b"]),
        ("fake-b ", ["fake-b "]),
        ("fake-b, fake-c", ["fake-b", " fake-c"]),
        (" ", [" "]),
        (["fake-b", 1], ["fake-b", 1]),
    ],
)
def test_fallback_parsers_preserve_literal_tokens(raw_value: object, expected: list[object]):
    eval_module = _load_code_module("eval_for_literal_fallback_parse", "eval.py")
    run_pipeline = _load_code_module(
        "run_pipeline_for_literal_fallback_parse",
        "run_pipeline.py",
    )

    assert eval_module.parse_fallback_models(raw_value) == expected
    assert run_pipeline.parse_eval_fallback_versions(raw_value) == expected


@pytest.mark.parametrize("raw_value", [None, 0, 1, True, False, b"fake-b", ("fake-b",)])
def test_fallback_parsers_reject_non_string_non_list_inputs(raw_value: object):
    eval_module = _load_code_module("eval_for_invalid_fallback_parse", "eval.py")
    run_pipeline = _load_code_module(
        "run_pipeline_for_invalid_fallback_parse",
        "run_pipeline.py",
    )

    with pytest.raises(ValueError):
        eval_module.parse_fallback_models(raw_value)
    with pytest.raises(ValueError):
        run_pipeline.parse_eval_fallback_versions(raw_value)


@pytest.mark.parametrize(
    "payload",
    [
        _completion_payload("missing", usage_marker="__missing__"),
        _completion_payload("null", usage_marker=None),
    ],
)
def test_eval_unknown_usage_is_aggregated_as_none_without_crash(
    monkeypatch,
    payload: dict[str, object],
) -> None:
    eval_module = _load_code_module("eval_for_unknown_usage", "eval.py")

    monkeypatch.setattr(
        eval_module,
        "api_call",
        lambda request_json: _DumpableCompletion(payload),
    )

    _, completion_json, generated_n = eval_module.run_completion_requests(
        "openai",
        "gpt-4.1-mini",
        [{"role": "user", "content": "score"}],
        1,
    )

    assert generated_n == 1
    assert completion_json["usage"] is None
    assert completion_json["responses"] == [payload]
    assert "index" not in completion_json["responses"][0]["choices"][0]
    assert completion_json["choices"][0]["index"] == 0


def test_eval_mixed_unknown_and_known_usage_merges_only_known_mapping(
    monkeypatch,
) -> None:
    eval_module = _load_code_module("eval_for_mixed_usage", "eval.py")
    responses = [
        _completion_payload("unknown", usage_marker=None),
        _completion_payload(
            "known",
            usage_marker={
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 1},
            },
        ),
    ]

    def fake_api_call(request_json: dict[str, object]) -> _DumpableCompletion:
        return _DumpableCompletion(responses.pop(0))

    monkeypatch.setattr(eval_module, "api_call", fake_api_call)

    _, completion_json, generated_n = eval_module.run_completion_requests(
        "openai",
        "gpt-4.1-mini",
        [{"role": "user", "content": "score"}],
        2,
    )

    assert generated_n == 2
    assert completion_json["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 1},
    }
    assert completion_json["responses"][0]["usage"] is None
    assert completion_json["responses"][1]["usage"]["prompt_tokens"] == 3
    assert [choice["index"] for choice in completion_json["choices"]] == [0, 1]


@pytest.mark.parametrize("usage_value", [[], "prompt_tokens=3"])
def test_eval_invalid_usage_type_raises_stable_redacted_error(
    monkeypatch,
    usage_value: object,
) -> None:
    eval_module = _load_code_module("eval_for_invalid_usage", "eval.py")
    payload = _completion_payload("invalid", usage_marker=usage_value)

    monkeypatch.setattr(
        eval_module,
        "api_call",
        lambda request_json: _DumpableCompletion(payload),
    )

    with pytest.raises(Exception) as exc_info:
        eval_module.run_completion_requests(
            "openai",
            "gpt-4.1-mini",
            [{"role": "user", "content": "score"}],
            1,
        )

    assert getattr(exc_info.value, "code", None) == "provider_response_usage_invalid"
    assert "openai" in str(exc_info.value)
    assert "gpt-4.1-mini" in str(exc_info.value)
    assert repr(usage_value) not in str(exc_info.value)
    assert "api_key" not in str(exc_info.value).lower()
    assert "authorization" not in str(exc_info.value).lower()


@pytest.mark.parametrize("completion_json", [{}, {"usage": None}, {"usage": {}}])
def test_cal_cost_keeps_unknown_usage_unavailable(completion_json: dict[str, object]) -> None:
    utils_module = _load_code_module("utils_for_unknown_usage_cost", "utils.py")

    cost = utils_module.cal_cost(completion_json, "gpt-4.1-mini")

    assert cost["prompt_tokens"] is None
    assert cost["actual_input_tokens"] is None
    assert cost["cached_tokens"] is None
    assert cost["output_tokens"] is None
    assert cost["input_cost"] is None
    assert cost["cached_input_cost"] is None
    assert cost["output_cost"] is None
    assert cost["total_cost"] is None


def test_print_log_cost_reports_unknown_usage_without_accumulating(
    tmp_path: Path,
    capsys,
) -> None:
    utils_module = _load_code_module("utils_for_unknown_usage_log", "utils.py")

    total = utils_module.print_log_cost(
        {},
        "gpt-4.1-mini",
        "eval",
        str(tmp_path),
        12.34,
    )

    captured = capsys.readouterr().out
    log_text = (tmp_path / "cost_info.log").read_text(encoding="utf-8")
    assert total == 12.34
    assert "Token usage: unavailable" in captured
    assert "Token usage: unavailable" in log_text
    assert "0 tokens" not in captured


def test_eval_short_choice_response_raises_stable_domain_error(monkeypatch) -> None:
    eval_module = _load_code_module("eval_for_short_choices", "eval.py")
    calls: list[dict[str, object]] = []

    def fake_api_call(request_json: dict[str, object]) -> _DumpableCompletion:
        calls.append(request_json)
        return _DumpableCompletion({"choices": [], "usage": {"prompt_tokens": 1}})

    monkeypatch.setattr(eval_module, "api_call", fake_api_call)

    with pytest.raises(Exception) as exc_info:
        eval_module.run_completion_requests(
            "openai",
            "gpt-4.1-mini",
            [{"role": "user", "content": "score"}],
            1,
        )

    assert getattr(exc_info.value, "code", None) == "provider_response_choice_count_mismatch"
    assert "gpt-4.1-mini" in str(exc_info.value)
    assert "api_key" not in str(exc_info.value).lower()
    assert "authorization" not in str(exc_info.value).lower()
    assert calls == [
        {
            "model": "gpt-4.1-mini",
            "messages": [{"role": "user", "content": "score"}],
            "temperature": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
        }
    ]


def test_kimi_k3_bundled_contract_has_empty_request_options() -> None:
    registry = ProviderRegistry.from_file(DEFAULT_REGISTRY_PATH)

    contract = registry.get("kimi", "kimi-k3")

    assert dict(contract.request_options) == {}


def test_kimi_k3_single_candidate_request_omits_fixed_parameters(monkeypatch) -> None:
    eval_module = _load_code_module("eval_for_kimi_k3_request", "eval.py")
    registry = ProviderRegistry.from_file(DEFAULT_REGISTRY_PATH)
    monkeypatch.setattr(eval_module, "get_provider_registry", lambda: registry)

    request_json = eval_module.build_request_json(
        "kimi",
        "kimi-k3",
        [{"role": "user", "content": "score"}],
        1,
    )

    assert request_json == {
        "model": "kimi-k3",
        "messages": [{"role": "user", "content": "score"}],
    }
    for forbidden in (
        "temperature",
        "top_p",
        "n",
        "presence_penalty",
        "frequency_penalty",
    ):
        assert forbidden not in request_json


def test_kimi_k3_delegate_kwargs_omit_fixed_parameters(monkeypatch) -> None:
    eval_module = _load_code_module("eval_for_kimi_k3_delegate", "eval.py")
    registry = ProviderRegistry.from_file(DEFAULT_REGISTRY_PATH)
    delegate = _FakeCompletionDelegate(
        [_completion_payload("ok", usage_marker={"prompt_tokens": 1})]
    )

    monkeypatch.setattr(eval_module, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(
        eval_module,
        "make_openai_client",
        lambda provider_id, model_id: _FakeChatClient(delegate),
    )

    request_json, completion_json, generated_n, actual_model, fallback_info = (
        eval_module.run_completion_requests_with_fallback(
            "kimi",
            "kimi-k3",
            [{"role": "user", "content": "score"}],
            1,
            [],
        )
    )

    assert generated_n == 1
    assert actual_model == "kimi-k3"
    assert fallback_info["fallback_used"] is False
    assert request_json == {
        "model": "kimi-k3",
        "messages": [{"role": "user", "content": "score"}],
    }
    assert completion_json["choices"][0]["index"] == 0
    assert delegate.calls == [request_json]
    for forbidden in (
        "temperature",
        "top_p",
        "n",
        "presence_penalty",
        "frequency_penalty",
    ):
        assert forbidden not in delegate.calls[0]


def test_synthetic_multi_candidate_request_keeps_explicit_n_when_supported(
    monkeypatch,
) -> None:
    eval_module = _load_code_module("eval_for_multi_candidate_n", "eval.py")
    registry = _synthetic_registry(max_n=2)
    monkeypatch.setattr(eval_module, "get_provider_registry", lambda: registry)

    request_json = eval_module.build_request_json(
        "fake",
        "fake-primary",
        [{"role": "user", "content": "score"}],
        2,
    )

    assert request_json["n"] == 2


def test_kimi_k3_generated_n_fans_out_without_explicit_n(monkeypatch) -> None:
    eval_module = _load_code_module("eval_for_kimi_k3_fanout", "eval.py")
    registry = _synthetic_registry(
        provider_id="kimi",
        primary_model_id="kimi-k3",
        fallback_model_ids=(),
        max_n=1,
        request_options={},
    )
    delegate = _FakeCompletionDelegate(
        [
            _completion_payload(
                "choice-a",
                usage_marker={
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 1},
                },
            ),
            _completion_payload(
                "choice-b",
                usage_marker={
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            ),
        ]
    )

    monkeypatch.setattr(eval_module, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(
        eval_module,
        "make_openai_client",
        lambda provider_id, model_id: _FakeChatClient(delegate),
    )

    _, completion_json, generated_n, actual_model, _ = (
        eval_module.run_completion_requests_with_fallback(
            "kimi",
            "kimi-k3",
            [{"role": "user", "content": "score"}],
            2,
            [],
        )
    )

    assert generated_n == 2
    assert actual_model == "kimi-k3"
    assert len(delegate.calls) == 2
    assert all("n" not in call for call in delegate.calls)
    assert [choice["index"] for choice in completion_json["choices"]] == [0, 1]
    assert completion_json["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 3},
    }


def test_quota_like_error_falls_back_to_qwen_plus(monkeypatch) -> None:
    eval_module = _load_code_module("eval_for_fallback_test", "eval.py")
    registry = _synthetic_registry(
        fallback_model_ids=("fake-fallback-a",),
    )
    calls = []

    class FakeQuotaError(Exception):
        status_code = 403

    def fake_make_client(provider_id, model_name):
        calls.append(("client", provider_id, model_name))
        return object()

    def fake_run_completion_requests(provider_id, model_name, msg, generated_n, input_tokens=None):
        calls.append(("run", provider_id, model_name))
        if model_name == "fake-primary":
            raise FakeQuotaError("AllocationQuota.FreeTierOnly")
        return (
            {"model": model_name, "n": generated_n},
            {"model": model_name, "choices": [], "usage": {}},
            generated_n,
        )

    monkeypatch.setattr(eval_module, "PermissionDeniedError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "BadRequestError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(eval_module, "make_openai_client", fake_make_client)
    monkeypatch.setattr(
        eval_module,
        "run_completion_requests",
        fake_run_completion_requests,
    )

    request_json, completion_json, generated_n, actual_model, fallback_info = (
        eval_module.run_completion_requests_with_fallback(
            "fake",
            "fake-primary",
            [{"role": "system", "content": "x"}],
            1,
            eval_module.default_fallback_models("fake", "fake-primary"),
        )
    )

    assert actual_model == "fake-fallback-a"
    assert request_json["model"] == "fake-fallback-a"
    assert completion_json["model"] == "fake-fallback-a"
    assert generated_n == 1
    assert fallback_info["fallback_used"] is True
    assert fallback_info["fallback_reason"] == "quota_like_error"
    assert fallback_info["fallback_from_model"] == "fake-primary"
    assert fallback_info["fallback_eval_model"] == "fake-fallback-a"
    assert fallback_info["fallback_remaining_models"] == []
    assert calls == [
        ("client", "fake", "fake-primary"),
        ("run", "fake", "fake-primary"),
        ("client", "fake", "fake-fallback-a"),
        ("run", "fake", "fake-fallback-a"),
    ]


def test_quota_like_error_keeps_remaining_fallback_chain(monkeypatch) -> None:
    eval_module = _load_code_module("eval_for_chain_fallback_test", "eval.py")
    registry = _synthetic_registry()
    calls = []

    class FakeQuotaError(Exception):
        status_code = 403

    def fake_make_client(provider_id, model_name):
        calls.append(("client", provider_id, model_name))
        return object()

    def fake_run_completion_requests(provider_id, model_name, msg, generated_n, input_tokens=None):
        calls.append(("run", provider_id, model_name))
        if model_name == "fake-primary":
            raise FakeQuotaError("AllocationQuota.FreeTierOnly")
        return (
            {"model": model_name, "n": generated_n},
            {"model": model_name, "choices": [], "usage": {}},
            generated_n,
        )

    monkeypatch.setattr(eval_module, "PermissionDeniedError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "BadRequestError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(eval_module, "make_openai_client", fake_make_client)
    monkeypatch.setattr(
        eval_module,
        "run_completion_requests",
        fake_run_completion_requests,
    )

    _, _, _, actual_model, fallback_info = (
        eval_module.run_completion_requests_with_fallback(
            "fake",
            "fake-primary",
            [{"role": "system", "content": "x"}],
            1,
            ["fake-fallback-a", "fake-fallback-b"],
        )
    )

    assert actual_model == "fake-fallback-a"
    assert fallback_info["fallback_used"] is True
    assert fallback_info["fallback_eval_model"] == "fake-fallback-a"
    assert fallback_info["fallback_model_chain"] == [
        "fake-primary",
        "fake-fallback-a",
        "fake-fallback-b",
    ]
    assert fallback_info["fallback_remaining_models"] == ["fake-fallback-b"]
    assert calls == [
        ("client", "fake", "fake-primary"),
        ("run", "fake", "fake-primary"),
        ("client", "fake", "fake-fallback-a"),
        ("run", "fake", "fake-fallback-a"),
    ]


def test_chained_quota_errors_fall_back_to_later_model(monkeypatch) -> None:
    eval_module = _load_code_module("eval_for_later_chain_fallback_test", "eval.py")
    registry = _synthetic_registry()
    calls = []

    class FakeQuotaError(Exception):
        status_code = 403

    def fake_make_client(provider_id, model_name):
        calls.append(("client", provider_id, model_name))
        return object()

    def fake_run_completion_requests(provider_id, model_name, msg, generated_n, input_tokens=None):
        calls.append(("run", provider_id, model_name))
        if model_name in {"fake-primary", "fake-fallback-a"}:
            raise FakeQuotaError("AllocationQuota.FreeTierOnly")
        return (
            {"model": model_name, "n": generated_n},
            {"model": model_name, "choices": [], "usage": {}},
            generated_n,
        )

    monkeypatch.setattr(eval_module, "PermissionDeniedError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "BadRequestError", FakeQuotaError)
    monkeypatch.setattr(eval_module, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(eval_module, "make_openai_client", fake_make_client)
    monkeypatch.setattr(
        eval_module,
        "run_completion_requests",
        fake_run_completion_requests,
    )

    request_json, _, _, actual_model, fallback_info = (
        eval_module.run_completion_requests_with_fallback(
            "fake",
            "fake-primary",
            [{"role": "system", "content": "x"}],
            1,
            ["fake-fallback-a", "fake-fallback-b"],
        )
    )

    assert actual_model == "fake-fallback-b"
    assert request_json["model"] == "fake-fallback-b"
    assert fallback_info["fallback_used"] is True
    assert fallback_info["fallback_from_model"] == "fake-fallback-a"
    assert fallback_info["fallback_eval_model"] == "fake-fallback-b"
    assert fallback_info["fallback_remaining_models"] == []
    assert calls == [
        ("client", "fake", "fake-primary"),
        ("run", "fake", "fake-primary"),
        ("client", "fake", "fake-fallback-a"),
        ("run", "fake", "fake-fallback-a"),
        ("client", "fake", "fake-fallback-b"),
        ("run", "fake", "fake-fallback-b"),
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
        provider="qwen",
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
        eval_provider="qwen",
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
        eval_gpt_version="fake-primary",
        eval_provider="fake",
        eval_fallback_gpt_versions="fake-fallback-a,fake-fallback-b",
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
    assert _arg_value(first_cmd, "--gpt_version") == "fake-primary"
    assert (
        _arg_value(first_cmd, "--fallback_gpt_versions")
        == "fake-fallback-a,fake-fallback-b"
    )

    status_path = tmp_path / "run_status.json"
    changed = run_pipeline.remember_fallback_eval_model(
        args,
        {
            "fallback_used": True,
            "fallback_reason": "quota_like_error",
            "fallback_from_model": "fake-primary",
            "fallback_eval_model": "fake-fallback-a",
            "eval_model": "fake-fallback-a",
            "fallback_model_chain": [
                "fake-primary",
                "fake-fallback-a",
                "fake-fallback-b",
            ],
            "fallback_remaining_models": ["fake-fallback-b"],
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
    assert _arg_value(next_cmd, "--gpt_version") == "fake-fallback-a"
    assert _arg_value(next_cmd, "--fallback_gpt_versions") == "fake-fallback-b"
    status = run_pipeline.load_json_file(str(status_path))
    assert status["requested_eval_model"] == "fake-primary"
    assert status["effective_eval_model"] == "fake-fallback-a"
    assert status["fallback_eval_model"] == "fake-fallback-a"
    assert status["fallback_from_model"] == "fake-primary"
    assert status["fallback_reason"] == "quota_like_error"
    assert status["eval_fallback_active"] is True
    assert status["remaining_eval_fallback_models"] == ["fake-fallback-b"]
    assert status["eval_fallback_model_chain"] == [
        "fake-primary",
        "fake-fallback-a",
        "fake-fallback-b",
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
        eval_gpt_version="fake-primary",
        eval_provider="fake",
        eval_fallback_gpt_versions="fake-fallback-a,fake-fallback-b",
        max_repair_rounds=2,
    )

    status_path = tmp_path / "run_status.json"
    changed = run_pipeline.remember_fallback_eval_model(
        args,
        {
            "fallback_used": False,
            "eval_model": "fake-primary",
            "fallback_model_chain": [
                "fake-primary",
                "fake-fallback-a",
                "fake-fallback-b",
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
    assert _arg_value(next_cmd, "--gpt_version") == "fake-primary"
    assert (
        _arg_value(next_cmd, "--fallback_gpt_versions")
        == "fake-fallback-a,fake-fallback-b"
    )
    assert not status_path.exists()


@pytest.mark.parametrize("generated_n", [0, 33])
def test_generated_n_boundaries_are_enforced(generated_n: int) -> None:
    run_pipeline = _load_code_module("run_pipeline_for_generated_bounds", "run_pipeline.py")
    args = SimpleNamespace(generated_n=generated_n, max_repair_rounds=3)

    with pytest.raises(ValueError, match="generated_n"):
        run_pipeline.validate_runtime_args(args)


@pytest.mark.parametrize(
    ("fallback_versions", "expected_code"),
    [
        ("fake-fallback-a,fake-fallback-a", "invalid_provider_fallbacks"),
        ("fake-primary", "invalid_provider_fallbacks"),
        (",", "invalid_provider_fallbacks"),
        (",fake-fallback-a", "invalid_provider_fallbacks"),
        ("fake-fallback-a,", "invalid_provider_fallbacks"),
        ("fake-fallback-a,,fake-fallback-b", "invalid_provider_fallbacks"),
        (" fake-fallback-a", "invalid_provider_fallbacks"),
        ("fake-fallback-a ", "invalid_provider_fallbacks"),
        ("fake-fallback-a, fake-fallback-b", "invalid_provider_fallbacks"),
        ("fake-missing", "unknown_model"),
        ("other-fallback", "unknown_model"),
    ],
)
def test_pipeline_cli_rejects_invalid_eval_fallback_topology(
    fallback_versions: str,
    expected_code: str,
    monkeypatch,
) -> None:
    run_pipeline = _load_code_module(
        "run_pipeline_for_invalid_fallback_topology",
        "run_pipeline.py",
    )
    registry = _synthetic_registry()
    monkeypatch.setattr(run_pipeline, "PROVIDER_REGISTRY", registry)
    args = SimpleNamespace(
        generated_n=1,
        max_repair_rounds=0,
        reproduce_provider="fake",
        reproduce_gpt_version="fake-primary",
        eval_provider="fake",
        eval_gpt_version="fake-primary",
        eval_fallback_gpt_versions=fallback_versions,
        checkpoint_mode="off",
        resume_from_stage="",
        resume_stage_sequence=0,
        resume_stage_attempt=0,
        checkpoint_recovery_count=0,
    )

    with pytest.raises(Exception) as exc_info:
        run_pipeline.validate_runtime_args(args)

    assert getattr(exc_info.value, "code", None) == expected_code


@pytest.mark.parametrize(
    ("fallback_models", "expected_code"),
    [
        (["fake-fallback-a", "fake-fallback-a"], "invalid_provider_fallbacks"),
        (["fake-primary"], "invalid_provider_fallbacks"),
        ([""], "invalid_provider_fallbacks"),
        ([" fake-fallback-a"], "invalid_provider_fallbacks"),
        (["fake-fallback-a "], "invalid_provider_fallbacks"),
        (["fake-fallback-a", " fake-fallback-b"], "invalid_provider_fallbacks"),
        (["fake-fallback-a", 1], "invalid_provider_fallbacks"),
        ("fake-fallback-a", "invalid_provider_fallbacks"),
        (["fake-missing"], "unknown_model"),
        (["other-fallback"], "unknown_model"),
    ],
)
def test_eval_rejects_invalid_fallback_chain_before_creating_client(
    monkeypatch,
    fallback_models: object,
    expected_code: str,
) -> None:
    eval_module = _load_code_module("eval_for_invalid_runtime_fallback", "eval.py")
    registry = _synthetic_registry()
    calls = []
    delegate = _FakeCompletionDelegate(
        [_completion_payload("unexpected", usage_marker={"prompt_tokens": 1})]
    )

    def fake_make_client(provider_id: str, model_name: str):
        calls.append((provider_id, model_name))
        return _FakeChatClient(delegate)

    monkeypatch.setattr(eval_module, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(eval_module, "make_openai_client", fake_make_client)

    with pytest.raises(Exception) as exc_info:
        eval_module.run_completion_requests_with_fallback(
            "fake",
            "fake-primary",
            [{"role": "user", "content": "score"}],
            1,
            fallback_models,
        )

    assert getattr(exc_info.value, "code", None) == expected_code
    assert calls == []
    assert delegate.calls == []
    assert eval_module.client is None


@pytest.mark.parametrize("max_repair_rounds", [-1, 11])
def test_max_repair_round_boundaries_are_enforced(max_repair_rounds: int) -> None:
    eval_module = _load_code_module("eval_for_repair_bounds", "eval.py")
    args = SimpleNamespace(generated_n=1, max_repair_rounds=max_repair_rounds)

    with pytest.raises(ValueError, match="max_repair_rounds"):
        eval_module.validate_eval_args(args)


def test_zero_repair_rounds_is_evaluate_only_policy() -> None:
    run_pipeline = _load_code_module("run_pipeline_for_zero_repair", "run_pipeline.py")
    args = SimpleNamespace(
        generated_n=1,
        max_repair_rounds=0,
        reproduce_provider="openai",
        reproduce_gpt_version="gpt-4.1-mini",
        eval_provider="qwen",
        eval_gpt_version="qwen3.7-max",
        eval_fallback_gpt_versions="",
    )

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
