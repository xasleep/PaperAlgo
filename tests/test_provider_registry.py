from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from codes.provider_registry import (
    DEFAULT_REGISTRY_PATH,
    REGISTRY_PATH_ENV,
    ProviderContractError,
    ProviderRegistry,
    create_registered_client,
)
from codes.utils import cal_cost


def _registry_mapping(*, max_n: int = 1, context_window: int | None = 32):
    return {
        "version": 1,
        "providers": {
            "fake": {
                "models": {
                    "fake-chat": {
                        "base_url": None,
                        "base_url_env": "FAKE_BASE_URL",
                        "api_key_env": "FAKE_API_KEY",
                        "max_n": max_n,
                        "context_window": context_window,
                        "max_output_tokens": 8,
                        "json_schema_support": True,
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
                        "request_options": {"temperature": 0},
                        "fallback_model_ids": [],
                    }
                }
            }
        },
    }


class _FakeCompletions:
    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 3},
        }

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _FakeClient:
    def __init__(self, outcome=None):
        self.completions = _FakeCompletions(outcome)
        self.chat = type("Chat", (), {"completions": self.completions})()


def _client(registry, fake_client, *, env=None):
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return fake_client

    client = create_registered_client(
        "fake",
        "fake-chat",
        registry=registry,
        environ=env
        or {"FAKE_API_KEY": "secret-value", "FAKE_BASE_URL": "https://fake.invalid/v1"},
        client_factory=factory,
    )
    return client, captured


def test_registered_client_routes_normal_request_and_keeps_secret_out_of_repr():
    registry = ProviderRegistry.from_mapping(_registry_mapping())
    fake = _FakeClient()
    client, captured = _client(registry, fake)

    response = client.chat.completions.create(
        model="fake-chat", messages=[{"role": "user", "content": "hello"}], n=1
    )

    assert response["choices"][0]["message"]["content"] == "ok"
    assert captured["api_key"] == "secret-value"
    assert captured["base_url"] == "https://fake.invalid/v1"
    assert captured["timeout"] == 2
    assert captured["max_retries"] == 0
    assert "secret-value" not in repr(client)


def test_max_n_one_rejects_multi_choice_before_transport():
    registry = ProviderRegistry.from_mapping(_registry_mapping(max_n=1))
    fake = _FakeClient()
    client, _ = _client(registry, fake)

    with pytest.raises(ProviderContractError) as exc_info:
        client.chat.completions.create(model="fake-chat", messages=[], n=2)

    assert exc_info.value.code == "provider_max_n_exceeded"
    assert fake.completions.calls == []


@pytest.mark.parametrize(
    ("provider_id", "model_id", "expected_code"),
    [
        ("missing", "fake-chat", "unknown_provider"),
        ("fake", "missing", "unknown_model"),
    ],
)
def test_unknown_provider_or_model_is_rejected(provider_id, model_id, expected_code):
    registry = ProviderRegistry.from_mapping(_registry_mapping())
    with pytest.raises(ProviderContractError) as exc_info:
        registry.resolve(
            provider_id,
            model_id,
            environ={"FAKE_API_KEY": "secret", "FAKE_BASE_URL": "https://fake.invalid/v1"},
        )
    assert exc_info.value.code == expected_code


def test_missing_base_url_and_key_are_distinct_safe_errors():
    registry = ProviderRegistry.from_mapping(_registry_mapping())

    with pytest.raises(ProviderContractError) as missing_key:
        registry.resolve("fake", "fake-chat", environ={"FAKE_BASE_URL": "https://fake.invalid/v1"})
    assert missing_key.value.code == "provider_api_key_missing"

    with pytest.raises(ProviderContractError) as missing_url:
        registry.resolve("fake", "fake-chat", environ={"FAKE_API_KEY": "secret-value"})
    assert missing_url.value.code == "provider_base_url_missing"
    assert "secret-value" not in str(missing_url.value)


class _FakeRateLimitError(RuntimeError):
    status_code = 429


@pytest.mark.parametrize(
    "outcome",
    [TimeoutError("timed out"), _FakeRateLimitError("rate limited")],
)
def test_transport_timeout_and_429_are_not_swallowed(outcome):
    registry = ProviderRegistry.from_mapping(_registry_mapping())
    fake = _FakeClient(outcome)
    client, _ = _client(registry, fake)

    with pytest.raises(type(outcome), match=str(outcome)):
        client.chat.completions.create(model="fake-chat", messages=[], n=1)


def test_partial_usage_is_returned_without_fabricating_cache_tokens(monkeypatch):
    monkeypatch.delenv(REGISTRY_PATH_ENV, raising=False)
    registry = ProviderRegistry.from_mapping(_registry_mapping())
    outcome = {"choices": [], "usage": {"prompt_tokens": 3}}
    fake = _FakeClient(outcome)
    client, _ = _client(registry, fake)

    response = client.chat.completions.create(model="fake-chat", messages=[], n=1)

    assert response["usage"] == {"prompt_tokens": 3}
    assert "cached_tokens" not in response["usage"]

    cost = cal_cost(response, "gpt-4.1-mini", "openai")
    assert cost["prompt_tokens"] == 3
    assert cost["output_tokens"] == 0
    assert cost["cached_tokens"] == 0
    assert cost["total_cost"] is None


def test_context_limit_is_enforced_before_transport():
    registry = ProviderRegistry.from_mapping(_registry_mapping(context_window=16))
    fake = _FakeClient()
    client, _ = _client(registry, fake)

    with pytest.raises(ProviderContractError) as exc_info:
        client.chat.completions.create(
            model="fake-chat", messages=[], n=1, _input_token_count=12, max_tokens=8
        )

    assert exc_info.value.code == "provider_context_limit_exceeded"
    assert fake.completions.calls == []


def test_model_is_bound_to_provider_even_when_another_provider_has_same_prefix():
    mapping = _registry_mapping()
    mapping["providers"]["other"] = {
        "models": {"fake-chat": dict(mapping["providers"]["fake"]["models"]["fake-chat"])}
    }
    registry = ProviderRegistry.from_mapping(mapping)

    assert registry.get("fake", "fake-chat").provider_id == "fake"
    assert registry.get("other", "fake-chat").provider_id == "other"


def test_every_remote_llm_entrypoint_builds_client_with_provider_and_model():
    codes_dir = Path(__file__).resolve().parents[1] / "codes"
    entrypoints = [
        "1_planning.py",
        "1.2_rag_config.py",
        "2_analyzing.py",
        "3_coding.py",
        "3.1_coding_sh.py",
        "4_debugging.py",
        "eval.py",
    ]

    for file_name in entrypoints:
        source = (codes_dir / file_name).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=file_name)
        client_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "make_openai_client"
        ]
        assert client_calls, file_name
        assert all(len(call.args) >= 2 for call in client_calls), file_name
        assert "from openai import OpenAI" not in source

    utils_source = (codes_dir / "utils.py").read_text(encoding="utf-8")
    assert "create_registered_client(provider_id, model_id)" in utils_source
    assert "model_name.startswith" not in utils_source


def test_eval_has_no_hard_coded_128k_remote_context_gate():
    eval_source = (
        Path(__file__).resolve().parents[1] / "codes" / "eval.py"
    ).read_text(encoding="utf-8")
    assert "128000" not in eval_source
    assert "validate_context" in eval_source


def test_bundled_registry_keeps_unverified_limits_and_prices_explicitly_unknown():
    registry = ProviderRegistry.from_file(DEFAULT_REGISTRY_PATH)
    deepseek = registry.get("deepseek", "deepseek-v4-pro")
    qwen = registry.get("qwen", "qwen3.7-max")
    openai = registry.get("openai", "gpt-4.1-mini")

    assert deepseek.api_key_env == "DEEPSEEK_API_KEY"
    assert qwen.api_key_env == "QWEN_API_KEY"
    assert openai.api_key_env == "OPENAI_API_KEY"
    assert len({deepseek.base_url_env, qwen.base_url_env, openai.base_url_env}) == 3
    for contract in (deepseek, qwen, openai):
        assert contract.base_url is None
        assert contract.context_window is None
        assert contract.max_output_tokens is None
        assert contract.pricing.status == "unknown"
        assert contract.pricing.effective_date is None
        assert contract.max_n == 1


def test_bundled_registry_model_allowlist_matches_curated_supported_ids():
    registry = ProviderRegistry.from_file(DEFAULT_REGISTRY_PATH)

    assert registry.model_ids("openai") == (
        "gpt-4.1-mini",
        "gpt-4o-mini",
    )
    assert registry.model_ids("deepseek") == (
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    )
    assert registry.model_ids("kimi") == (
        "kimi-k2.6",
        "kimi-k2.7-code",
        "kimi-k2.7-code-highspeed",
        "kimi-k3",
    )
    assert registry.model_ids("qwen") == (
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.8-max",
    )
    assert registry.model_ids("claude") == ()


@pytest.mark.parametrize(
    ("provider_id", "retired_or_invalid_model_id"),
    [
        ("deepseek", "deepseek-chat"),
        ("deepseek", "deepseek-reasoner"),
        ("qwen", "qwen-3.7-max"),
        ("qwen", "qwen-3.7-plus"),
        ("qwen", "qwen-3.8-max"),
        ("openai", "o3-mini"),
        ("openai", "o4-mini"),
        ("kimi", "kimi-k2.5"),
        ("kimi", "moonshot-v1-8k"),
    ],
)
def test_bundled_registry_rejects_retired_deprecated_or_unverified_model_ids(
    provider_id: str,
    retired_or_invalid_model_id: str,
):
    registry = ProviderRegistry.from_file(DEFAULT_REGISTRY_PATH)

    with pytest.raises(ProviderContractError) as exc_info:
        registry.get(provider_id, retired_or_invalid_model_id)

    assert exc_info.value.code == "unknown_model"


@pytest.mark.parametrize("model_id", ["deepseek-v4-pro", "deepseek-v4-flash"])
def test_bundled_registry_keeps_current_deepseek_v4_models(model_id: str):
    registry = ProviderRegistry.from_file(DEFAULT_REGISTRY_PATH)

    assert registry.get("deepseek", model_id).model_id == model_id


@pytest.mark.parametrize("payload", [None, [], "registry", 1, True])
def test_registry_root_must_be_an_object_with_a_stable_error(payload):
    with pytest.raises(ProviderContractError) as exc_info:
        ProviderRegistry.from_mapping(payload)  # type: ignore[arg-type]

    assert exc_info.value.code == "invalid_provider_registry"
    assert exc_info.value.safe_details == {"field": "registry"}


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_registry_rejects_non_strict_version_types_without_leaking_raw_value(version):
    mapping = _registry_mapping()
    mapping["version"] = version

    with pytest.raises(ProviderContractError) as exc_info:
        ProviderRegistry.from_mapping(mapping)

    assert exc_info.value.code == "invalid_provider_registry"
    assert exc_info.value.safe_details == {"field": "version"}
    assert repr(version) not in str(exc_info.value)
    assert repr(version) not in repr(exc_info.value)


@pytest.mark.parametrize(
    ("request_options", "expected_field", "raw_value"),
    [
        (
            {"response_format": {"type": []}},
            "request_options.response_format.type",
            [],
        ),
        (
            {"response_format": {"type": {}}},
            "request_options.response_format.type",
            {},
        ),
        ({"reasoning_effort": []}, "request_options.reasoning_effort", []),
        ({"reasoning_effort": {}}, "request_options.reasoning_effort", {}),
    ],
)
def test_registry_rejects_request_option_membership_types_without_typeerror(
    request_options: dict[str, object],
    expected_field: str,
    raw_value: object,
):
    mapping = _registry_mapping()
    mapping["providers"]["fake"]["models"]["fake-chat"][
        "request_options"
    ] = request_options

    with pytest.raises(ProviderContractError) as exc_info:
        ProviderRegistry.from_mapping(mapping)

    assert exc_info.value.code == "invalid_provider_registry"
    assert exc_info.value.safe_details["field"] == expected_field
    assert repr(raw_value) not in str(exc_info.value)
    assert repr(raw_value) not in repr(exc_info.value)


@pytest.mark.parametrize("pricing_status", [[], {}])
def test_registry_rejects_pricing_status_membership_types_without_typeerror(
    pricing_status: object,
):
    mapping = _registry_mapping()
    mapping["providers"]["fake"]["models"]["fake-chat"]["pricing"][
        "status"
    ] = pricing_status

    with pytest.raises(ProviderContractError) as exc_info:
        ProviderRegistry.from_mapping(mapping)

    assert exc_info.value.code == "invalid_provider_registry"
    assert exc_info.value.safe_details["field"] == "pricing.status"
    assert repr(pricing_status) not in str(exc_info.value)
    assert repr(pricing_status) not in repr(exc_info.value)


@pytest.mark.parametrize("level", ["root", "provider", "model", "pricing"])
def test_registry_rejects_unknown_fields_at_every_schema_level(level: str):
    mapping = _registry_mapping()
    model = mapping["providers"]["fake"]["models"]["fake-chat"]
    targets = {
        "root": mapping,
        "provider": mapping["providers"]["fake"],
        "model": model,
        "pricing": model["pricing"],
    }
    targets[level]["unexpected_secret_like_field"] = "must-not-be-ignored"

    with pytest.raises(ProviderContractError) as exc_info:
        ProviderRegistry.from_mapping(mapping)

    assert exc_info.value.code == "invalid_provider_registry"
    assert "must-not-be-ignored" not in str(exc_info.value)
    assert "must-not-be-ignored" not in repr(exc_info.value)


@pytest.mark.parametrize(
    "request_options",
    [
        {"unknown_option": 1},
        {"temperature": {}},
        {"temperature": "0"},
        {"temperature": True},
        {"temperature": float("nan")},
        {"temperature": float("inf")},
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"top_p": -0.1},
        {"top_p": 1.1},
        {"max_tokens": True},
        {"max_tokens": 0},
        {"max_tokens": -1},
        {"max_completion_tokens": False},
        {"reasoning_effort": "turbo"},
        {"stop": ["ok", 1]},
        {"stop": []},
        {"response_format": {"type": "json_schema"}},
        {"response_format": {"type": "xml"}},
    ],
)
def test_registry_validates_request_options_types_ranges_and_structure(
    request_options: dict[str, object],
):
    mapping = _registry_mapping()
    mapping["providers"]["fake"]["models"]["fake-chat"][
        "request_options"
    ] = request_options

    with pytest.raises(ProviderContractError) as exc_info:
        ProviderRegistry.from_mapping(mapping)

    assert exc_info.value.code == "invalid_provider_registry"
    assert exc_info.value.safe_details["field"].startswith("request_options")


@pytest.mark.parametrize("field_name", ["timeout_seconds", "max_n", "max_concurrency"])
@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf")])
def test_registry_rejects_bool_and_non_finite_required_numbers(
    field_name: str,
    invalid: object,
):
    mapping = _registry_mapping()
    mapping["providers"]["fake"]["models"]["fake-chat"][field_name] = invalid

    with pytest.raises(ProviderContractError) as exc_info:
        ProviderRegistry.from_mapping(mapping)

    assert exc_info.value.code == "invalid_provider_registry"


def _add_fallback_model(mapping: dict, model_id: str, *, provider_id: str = "fake") -> None:
    source = mapping["providers"]["fake"]["models"]["fake-chat"]
    provider = mapping["providers"].setdefault(provider_id, {"models": {}})
    provider["models"][model_id] = copy.deepcopy(source)
    provider["models"][model_id]["fallback_model_ids"] = []


def test_validate_fallback_chain_returns_ordered_tuple_for_valid_models():
    mapping = _registry_mapping()
    _add_fallback_model(mapping, "fake-fallback-a")
    _add_fallback_model(mapping, "fake-fallback-b")
    registry = ProviderRegistry.from_mapping(mapping)

    assert registry.validate_fallback_chain(
        "fake",
        "fake-chat",
        ["fake-fallback-a", "fake-fallback-b"],
    ) == ("fake-fallback-a", "fake-fallback-b")


@pytest.mark.parametrize(
    ("fallback_model_ids", "expected_code"),
    [
        (["fake-fallback", "fake-fallback"], "invalid_provider_fallbacks"),
        (["fake-chat"], "invalid_provider_fallbacks"),
        ("fake-fallback", "invalid_provider_fallbacks"),
        (b"fake-fallback", "invalid_provider_fallbacks"),
        (None, "invalid_provider_fallbacks"),
        ([1], "invalid_provider_fallbacks"),
        ([""], "invalid_provider_fallbacks"),
        ([" fake-fallback"], "invalid_provider_fallbacks"),
        (["fake-fallback "], "invalid_provider_fallbacks"),
        (["missing-chat"], "unknown_model"),
    ],
)
def test_validate_fallback_chain_rejects_invalid_runtime_topologies(
    fallback_model_ids: object,
    expected_code: str,
):
    mapping = _registry_mapping()
    _add_fallback_model(mapping, "fake-fallback")
    registry = ProviderRegistry.from_mapping(mapping)

    with pytest.raises(ProviderContractError) as exc_info:
        registry.validate_fallback_chain(  # type: ignore[arg-type]
            "fake",
            "fake-chat",
            fallback_model_ids,
        )

    assert exc_info.value.code == expected_code
    if expected_code == "invalid_provider_fallbacks":
        assert exc_info.value.safe_details["field"] == "fallback_model_ids"


def test_validate_fallback_chain_rejects_model_from_another_provider():
    mapping = _registry_mapping()
    _add_fallback_model(mapping, "other-chat", provider_id="other")
    registry = ProviderRegistry.from_mapping(mapping)

    with pytest.raises(ProviderContractError) as exc_info:
        registry.validate_fallback_chain("fake", "fake-chat", ["other-chat"])

    assert exc_info.value.code == "unknown_model"
    assert exc_info.value.safe_details == {
        "provider_id": "fake",
        "model_id": "other-chat",
    }


@pytest.mark.parametrize("case", ["duplicate", "self", "cross_provider", "missing"])
def test_registry_fallbacks_are_unique_non_self_and_same_provider(case: str):
    mapping = _registry_mapping()
    model = mapping["providers"]["fake"]["models"]["fake-chat"]
    if case == "duplicate":
        _add_fallback_model(mapping, "fake-fallback")
        model["fallback_model_ids"] = ["fake-fallback", "fake-fallback"]
    elif case == "self":
        model["fallback_model_ids"] = ["fake-chat"]
    elif case == "cross_provider":
        _add_fallback_model(mapping, "other-chat", provider_id="other")
        model["fallback_model_ids"] = ["other-chat"]
    else:
        model["fallback_model_ids"] = ["missing-chat"]

    with pytest.raises(ProviderContractError) as exc_info:
        ProviderRegistry.from_mapping(mapping)

    assert exc_info.value.code == "invalid_provider_registry"
    assert exc_info.value.safe_details["field"] == "fallback_model_ids"


def test_registry_file_parse_and_io_errors_are_stable_and_redacted(tmp_path: Path):
    secret = "sk-registry-secret-that-must-not-leak"
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"version": 1, "api_key": "' + secret + '"', encoding="utf-8")

    for path in (malformed, tmp_path / "missing.json"):
        with pytest.raises(ProviderContractError) as exc_info:
            ProviderRegistry.from_file(path)
        assert exc_info.value.code == "invalid_provider_registry"
        assert exc_info.value.safe_details == {"field": "registry"}
        assert secret not in str(exc_info.value)
        assert secret not in repr(exc_info.value)


def test_registry_file_rejects_non_standard_nan_constant(tmp_path: Path):
    mapping = _registry_mapping()
    encoded = json.dumps(mapping).replace('"temperature": 0', '"temperature": NaN')
    path = tmp_path / "nan.json"
    path.write_text(encoded, encoding="utf-8")

    with pytest.raises(ProviderContractError) as exc_info:
        ProviderRegistry.from_file(path)

    assert exc_info.value.code == "invalid_provider_registry"


def test_model_contract_request_options_are_deeply_immutable():
    mapping = _registry_mapping()
    mapping["providers"]["fake"]["models"]["fake-chat"]["request_options"] = {
        "stop": ["END"],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "strict": True,
                "schema": {"type": "object", "required": ["answer"]},
            },
        },
    }
    contract = ProviderRegistry.from_mapping(mapping).get("fake", "fake-chat")

    with pytest.raises(TypeError):
        contract.request_options["temperature"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        contract.request_options["response_format"]["type"] = "text"  # type: ignore[index]
    with pytest.raises((AttributeError, TypeError)):
        contract.request_options["stop"].append("MORE")  # type: ignore[union-attr]
