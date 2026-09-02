"""Versioned, explicit provider/model contracts for remote LLM requests.

The registry stores environment variable *names*, never credentials.  Runtime
credentials are resolved only when a client is created and are deliberately
excluded from representations and error messages.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import threading
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit


REGISTRY_PATH_ENV = "PAPER2CODE_PROVIDER_REGISTRY_PATH"
DEFAULT_REGISTRY_PATH = Path(__file__).with_name("providers.v1.json")
_UNSET = object()
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_REQUEST_OPTION_KEYS = frozenset(
    {
        "frequency_penalty",
        "max_completion_tokens",
        "max_tokens",
        "presence_penalty",
        "reasoning_effort",
        "response_format",
        "stop",
        "temperature",
        "top_p",
    }
)
_ROOT_FIELDS = frozenset({"version", "providers"})
_PROVIDER_FIELDS = frozenset({"models"})
_MODEL_FIELDS = frozenset(
    {
        "base_url",
        "base_url_env",
        "api_key_env",
        "max_n",
        "context_window",
        "max_output_tokens",
        "json_schema_support",
        "usage_support",
        "cache_token_support",
        "timeout_seconds",
        "max_retries",
        "max_concurrency",
        "pricing",
        "request_options",
        "fallback_model_ids",
    }
)
_PRICING_FIELDS = frozenset(
    {
        "status",
        "currency",
        "input_per_million",
        "cached_input_per_million",
        "output_per_million",
        "effective_date",
    }
)
_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})


class ProviderContractError(ValueError):
    """A stable, non-secret provider contract failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        field_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id
        self.model_id = model_id
        self.field_name = field_name

    @property
    def safe_details(self) -> dict[str, str]:
        details = {}
        if self.provider_id is not None:
            details["provider_id"] = self.provider_id
        if self.model_id is not None:
            details["model_id"] = self.model_id
        if self.field_name is not None:
            details["field"] = self.field_name
        return details


@dataclass(frozen=True)
class PricingContract:
    status: str
    currency: str | None
    input_per_million: float | None
    cached_input_per_million: float | None
    output_per_million: float | None
    effective_date: str | None


@dataclass(frozen=True)
class ModelContract:
    provider_id: str
    model_id: str
    base_url: str | None
    base_url_env: str
    api_key_env: str
    max_n: int
    context_window: int | None
    max_output_tokens: int | None
    json_schema_support: bool | None
    usage_support: bool | None
    cache_token_support: bool | None
    timeout_seconds: float
    max_retries: int
    max_concurrency: int
    pricing: PricingContract
    request_options: Mapping[str, Any] = field(default_factory=dict)
    fallback_model_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedModelContract:
    model: ModelContract
    api_key: str = field(repr=False)
    base_url: str

    @property
    def provider_id(self) -> str:
        return self.model.provider_id

    @property
    def model_id(self) -> str:
        return self.model.model_id


def _contract_error(
    code: str,
    message: str,
    provider_id: str | None = None,
    model_id: str | None = None,
    field_name: str | None = None,
) -> ProviderContractError:
    return ProviderContractError(
        code,
        message,
        provider_id=provider_id,
        model_id=model_id,
        field_name=field_name,
    )


def _invalid_registry(
    message: str,
    provider_id: str | None = None,
    model_id: str | None = None,
    field_name: str = "registry",
) -> ProviderContractError:
    return _contract_error(
        "invalid_provider_registry",
        message,
        provider_id,
        model_id,
        field_name,
    )


def _reject_unknown_fields(
    mapping: Mapping[str, Any],
    allowed: frozenset[str],
    *,
    provider_id: str | None = None,
    model_id: str | None = None,
    field_name: str,
) -> None:
    if not all(isinstance(name, str) for name in mapping):
        raise _invalid_registry(
            "Provider registry object field names must be strings.",
            provider_id,
            model_id,
            field_name,
        )
    if set(mapping) - allowed:
        raise _invalid_registry(
            "Provider registry contains unsupported fields.",
            provider_id,
            model_id,
            field_name,
        )


def _finite_number(
    value: Any,
    *,
    minimum: float,
    maximum: float,
    provider_id: str,
    model_id: str,
    field_name: str,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
        or value > maximum
    ):
        raise _invalid_registry(
            "Provider registry numeric field is outside its safe range.",
            provider_id,
            model_id,
            field_name,
        )
    return float(value)


def _validate_json_value(
    value: Any,
    *,
    provider_id: str,
    model_id: str,
    field_name: str,
) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise _invalid_registry(
            "JSON values in request_options must be finite.",
            provider_id,
            model_id,
            field_name,
        )
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise _invalid_registry(
                "JSON object keys in request_options must be strings.",
                provider_id,
                model_id,
                field_name,
            )
        for nested in value.values():
            _validate_json_value(
                nested,
                provider_id=provider_id,
                model_id=model_id,
                field_name=field_name,
            )
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _validate_json_value(
                nested,
                provider_id=provider_id,
                model_id=model_id,
                field_name=field_name,
            )
        return
    raise _invalid_registry(
        "request_options must contain JSON-compatible values.",
        provider_id,
        model_id,
        field_name,
    )


def _validate_response_format(
    value: Any,
    *,
    provider_id: str,
    model_id: str,
) -> None:
    field_name = "request_options.response_format"
    if not isinstance(value, Mapping):
        raise _invalid_registry(
            "response_format must be an object.", provider_id, model_id, field_name
        )
    response_type = value.get("type")
    if not isinstance(response_type, str):
        raise _invalid_registry(
            "response_format type is not supported.",
            provider_id,
            model_id,
            "request_options.response_format.type",
        )
    if response_type in {"text", "json_object"}:
        if set(value) != {"type"}:
            raise _invalid_registry(
                "response_format contains unsupported fields.",
                provider_id,
                model_id,
                field_name,
            )
        return
    if response_type != "json_schema" or set(value) != {"type", "json_schema"}:
        raise _invalid_registry(
            "response_format type is not supported.", provider_id, model_id, field_name
        )
    schema_wrapper = value.get("json_schema")
    if not isinstance(schema_wrapper, Mapping):
        raise _invalid_registry(
            "response_format.json_schema must be an object.",
            provider_id,
            model_id,
            field_name,
        )
    allowed = {"name", "description", "schema", "strict"}
    if set(schema_wrapper) - allowed or not {"name", "schema"}.issubset(schema_wrapper):
        raise _invalid_registry(
            "response_format.json_schema has invalid fields.",
            provider_id,
            model_id,
            field_name,
        )
    if not isinstance(schema_wrapper["name"], str) or not schema_wrapper["name"].strip():
        raise _invalid_registry(
            "response_format.json_schema.name must be a non-empty string.",
            provider_id,
            model_id,
            field_name,
        )
    if not isinstance(schema_wrapper["schema"], Mapping):
        raise _invalid_registry(
            "response_format.json_schema.schema must be an object.",
            provider_id,
            model_id,
            field_name,
        )
    if "description" in schema_wrapper and not isinstance(
        schema_wrapper["description"], str
    ):
        raise _invalid_registry(
            "response_format.json_schema.description must be a string.",
            provider_id,
            model_id,
            field_name,
        )
    if "strict" in schema_wrapper and not isinstance(schema_wrapper["strict"], bool):
        raise _invalid_registry(
            "response_format.json_schema.strict must be boolean.",
            provider_id,
            model_id,
            field_name,
        )
    _validate_json_value(
        schema_wrapper["schema"],
        provider_id=provider_id,
        model_id=model_id,
        field_name=field_name,
    )


def _validate_request_options(
    value: Any,
    *,
    provider_id: str,
    model_id: str,
) -> Mapping[str, Any]:
    field_name = "request_options"
    if not isinstance(value, Mapping):
        raise _invalid_registry(
            "request_options must be an object.", provider_id, model_id, field_name
        )
    if not all(isinstance(key, str) for key in value) or set(value) - _REQUEST_OPTION_KEYS:
        raise _invalid_registry(
            "Provider request_options contains unsupported fields.",
            provider_id,
            model_id,
            field_name,
        )
    for name, option in value.items():
        option_field = f"request_options.{name}"
        if name == "temperature":
            _finite_number(
                option,
                minimum=0,
                maximum=2,
                provider_id=provider_id,
                model_id=model_id,
                field_name=option_field,
            )
        elif name == "top_p":
            _finite_number(
                option,
                minimum=0,
                maximum=1,
                provider_id=provider_id,
                model_id=model_id,
                field_name=option_field,
            )
        elif name in {"frequency_penalty", "presence_penalty"}:
            _finite_number(
                option,
                minimum=-2,
                maximum=2,
                provider_id=provider_id,
                model_id=model_id,
                field_name=option_field,
            )
        elif name in {"max_tokens", "max_completion_tokens"}:
            if isinstance(option, bool) or not isinstance(option, int) or option <= 0:
                raise _invalid_registry(
                    "Token limits in request_options must be positive integers.",
                    provider_id,
                    model_id,
                    option_field,
                )
        elif name == "reasoning_effort":
            if not isinstance(option, str) or option not in _REASONING_EFFORTS:
                raise _invalid_registry(
                    "reasoning_effort is not supported.",
                    provider_id,
                    model_id,
                    option_field,
                )
        elif name == "stop":
            if isinstance(option, str):
                valid_stop = bool(option)
            else:
                valid_stop = (
                    isinstance(option, (list, tuple))
                    and 1 <= len(option) <= 4
                    and all(isinstance(item, str) and bool(item) for item in option)
                )
            if not valid_stop:
                raise _invalid_registry(
                    "stop must be a string or one to four non-empty strings.",
                    provider_id,
                    model_id,
                    option_field,
                )
        elif name == "response_format":
            _validate_response_format(
                option, provider_id=provider_id, model_id=model_id
            )
    return _deep_freeze(value)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


def _model_contract_payload(contract: ModelContract) -> dict[str, Any]:
    return {
        "provider_id": contract.provider_id,
        "model_id": contract.model_id,
        "base_url": contract.base_url,
        "base_url_env": contract.base_url_env,
        "api_key_env": contract.api_key_env,
        "max_n": contract.max_n,
        "context_window": contract.context_window,
        "max_output_tokens": contract.max_output_tokens,
        "json_schema_support": contract.json_schema_support,
        "usage_support": contract.usage_support,
        "cache_token_support": contract.cache_token_support,
        "timeout_seconds": contract.timeout_seconds,
        "max_retries": contract.max_retries,
        "max_concurrency": contract.max_concurrency,
        "pricing": {
            "status": contract.pricing.status,
            "currency": contract.pricing.currency,
            "input_per_million": contract.pricing.input_per_million,
            "cached_input_per_million": contract.pricing.cached_input_per_million,
            "output_per_million": contract.pricing.output_per_million,
            "effective_date": contract.pricing.effective_date,
        },
        "request_options": _deep_thaw(contract.request_options),
        "fallback_model_ids": list(contract.fallback_model_ids),
    }


def _required(mapping: Mapping[str, Any], name: str, provider_id: str, model_id: str) -> Any:
    if name not in mapping:
        raise _contract_error(
            "invalid_provider_registry",
            f"Provider registry model is missing required field '{name}'.",
            provider_id,
            model_id,
            name,
        )
    return mapping[name]


def _optional_positive_int(value: Any, name: str, provider_id: str, model_id: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _contract_error(
            "invalid_provider_registry",
            f"Provider registry field '{name}' must be a positive integer or null.",
            provider_id,
            model_id,
            name,
        )
    return value


def _optional_bool(value: Any, name: str, provider_id: str, model_id: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise _contract_error(
            "invalid_provider_registry",
            f"Provider registry field '{name}' must be boolean or null.",
            provider_id,
            model_id,
            name,
        )
    return value


def _validate_base_url(value: str, provider_id: str, model_id: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise _contract_error(
            "provider_base_url_invalid",
            "Provider base URL must be an HTTP(S) origin/path without credentials.",
            provider_id,
            model_id,
            "base_url",
        )
    return value.rstrip("/")


class ProviderRegistry:
    def __init__(self, version: int, contracts: Mapping[tuple[str, str], ModelContract]):
        self.version = version
        self._contracts = MappingProxyType(dict(contracts))
        self._provider_ids = frozenset(provider_id for provider_id, _ in contracts)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "ProviderRegistry":
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(
                    handle,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError("non-finite JSON constant")
                    ),
                )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise _invalid_registry(
                "Provider registry could not be read as valid JSON."
            ) from exc
        return cls.from_mapping(payload)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | object) -> "ProviderRegistry":
        if not isinstance(payload, Mapping):
            raise _invalid_registry("Provider registry root must be an object.")
        _reject_unknown_fields(payload, _ROOT_FIELDS, field_name="registry")
        version = payload.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
            raise _contract_error(
                "invalid_provider_registry",
                "Provider registry version must be 1.",
                field_name="version",
            )
        providers = payload.get("providers")
        if not isinstance(providers, Mapping) or not providers:
            raise _contract_error(
                "invalid_provider_registry",
                "Provider registry must contain a non-empty providers mapping.",
                field_name="providers",
            )

        contracts: dict[tuple[str, str], ModelContract] = {}
        declared_providers: set[str] = set()
        for provider_id, provider_payload in providers.items():
            if not isinstance(provider_id, str) or _ID_PATTERN.fullmatch(provider_id) is None:
                raise _contract_error(
                    "invalid_provider_registry",
                    "Provider IDs must be non-empty strings.",
                    field_name="provider_id",
                )
            declared_providers.add(provider_id)
            if not isinstance(provider_payload, Mapping):
                raise _contract_error(
                    "invalid_provider_registry",
                    "Provider entry must be an object.",
                    provider_id,
                )
            _reject_unknown_fields(
                provider_payload,
                _PROVIDER_FIELDS,
                provider_id=provider_id,
                field_name="provider",
            )
            models = provider_payload.get("models")
            if not isinstance(models, Mapping):
                raise _contract_error(
                    "invalid_provider_registry",
                    "Provider models must be an object.",
                    provider_id,
                    field_name="models",
                )
            for model_id, model_payload in models.items():
                if not isinstance(model_id, str) or _ID_PATTERN.fullmatch(model_id) is None:
                    raise _contract_error(
                        "invalid_provider_registry",
                        "Model IDs must be non-empty strings.",
                        provider_id,
                        field_name="model_id",
                    )
                if not isinstance(model_payload, Mapping):
                    raise _contract_error(
                        "invalid_provider_registry",
                        "Model entry must be an object.",
                        provider_id,
                        model_id,
                    )
                _reject_unknown_fields(
                    model_payload,
                    _MODEL_FIELDS,
                    provider_id=provider_id,
                    model_id=model_id,
                    field_name="model",
                )

                base_url = _required(model_payload, "base_url", provider_id, model_id)
                if base_url is not None:
                    if not isinstance(base_url, str) or not base_url.strip():
                        raise _contract_error(
                            "invalid_provider_registry",
                            "Configured base_url must be a non-empty string or null.",
                            provider_id,
                            model_id,
                            "base_url",
                        )
                    base_url = _validate_base_url(base_url.strip(), provider_id, model_id)

                api_key_env = _required(model_payload, "api_key_env", provider_id, model_id)
                base_url_env = _required(model_payload, "base_url_env", provider_id, model_id)
                for name, value in (("api_key_env", api_key_env), ("base_url_env", base_url_env)):
                    if not isinstance(value, str) or not value or not value.replace("_", "").isalnum() or value.upper() != value:
                        raise _contract_error(
                            "invalid_provider_registry",
                            f"Provider registry field '{name}' must be an uppercase environment name.",
                            provider_id,
                            model_id,
                            name,
                        )

                max_n = _optional_positive_int(
                    _required(model_payload, "max_n", provider_id, model_id),
                    "max_n",
                    provider_id,
                    model_id,
                )
                if max_n is None:
                    raise _contract_error(
                        "invalid_provider_registry",
                        "Provider max_n cannot be unknown.",
                        provider_id,
                        model_id,
                        "max_n",
                    )
                timeout_seconds = _required(model_payload, "timeout_seconds", provider_id, model_id)
                timeout_seconds = _finite_number(
                    timeout_seconds,
                    minimum=0.001,
                    maximum=3600,
                    provider_id=provider_id,
                    model_id=model_id,
                    field_name="timeout_seconds",
                )
                max_retries = _required(model_payload, "max_retries", provider_id, model_id)
                if (
                    isinstance(max_retries, bool)
                    or not isinstance(max_retries, int)
                    or not 0 <= max_retries <= 10
                ):
                    raise _contract_error(
                        "invalid_provider_registry",
                        "Provider max_retries must be an integer between 0 and 10.",
                        provider_id,
                        model_id,
                        "max_retries",
                    )
                max_concurrency = _optional_positive_int(
                    _required(model_payload, "max_concurrency", provider_id, model_id),
                    "max_concurrency",
                    provider_id,
                    model_id,
                )
                if max_concurrency is None:
                    raise _contract_error(
                        "invalid_provider_registry",
                        "Provider max_concurrency cannot be unknown.",
                        provider_id,
                        model_id,
                        "max_concurrency",
                    )

                pricing_payload = _required(model_payload, "pricing", provider_id, model_id)
                if not isinstance(pricing_payload, Mapping):
                    raise _contract_error(
                        "invalid_provider_registry",
                        "Provider pricing must be an object.",
                        provider_id,
                        model_id,
                        "pricing",
                    )
                _reject_unknown_fields(
                    pricing_payload,
                    _PRICING_FIELDS,
                    provider_id=provider_id,
                    model_id=model_id,
                    field_name="pricing",
                )
                pricing_values = {
                    name: _required(pricing_payload, name, provider_id, model_id)
                    for name in _PRICING_FIELDS
                }
                pricing_status = pricing_values["status"]
                if not isinstance(pricing_status, str) or pricing_status not in {
                    "unknown",
                    "configured",
                }:
                    raise _contract_error(
                        "invalid_provider_registry",
                        "Pricing status must be 'unknown' or 'configured'.",
                        provider_id,
                        model_id,
                        "pricing.status",
                    )
                if pricing_values["status"] == "unknown" and any(
                    pricing_values[name] is not None
                    for name in _PRICING_FIELDS
                    if name != "status"
                ):
                    raise _contract_error(
                        "invalid_provider_registry",
                        "Unknown pricing must not contain guessed values.",
                        provider_id,
                        model_id,
                        "pricing",
                    )
                if pricing_values["status"] == "configured" and (
                    not isinstance(pricing_values["currency"], str)
                    or not pricing_values["currency"]
                    or not isinstance(pricing_values["effective_date"], str)
                    or not pricing_values["effective_date"]
                    or pricing_values["input_per_million"] is None
                    or pricing_values["output_per_million"] is None
                ):
                    raise _contract_error(
                        "invalid_provider_registry",
                        "Configured pricing requires currency, effective date, input, and output prices.",
                        provider_id,
                        model_id,
                        "pricing",
                    )
                if pricing_values["status"] == "configured":
                    try:
                        date.fromisoformat(pricing_values["effective_date"])
                    except ValueError as exc:
                        raise _invalid_registry(
                            "Configured pricing effective_date must use YYYY-MM-DD.",
                            provider_id,
                            model_id,
                            "pricing.effective_date",
                        ) from exc
                    for name in (
                        "input_per_million",
                        "cached_input_per_million",
                        "output_per_million",
                    ):
                        value = pricing_values[name]
                        if value is not None and (
                            isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(value)
                            or value < 0
                        ):
                            raise _contract_error(
                                "invalid_provider_registry",
                                "Configured prices must be non-negative numbers or null.",
                                provider_id,
                                model_id,
                                f"pricing.{name}",
                            )

                request_options = _validate_request_options(
                    _required(model_payload, "request_options", provider_id, model_id),
                    provider_id=provider_id,
                    model_id=model_id,
                )
                fallback_model_ids = _required(
                    model_payload, "fallback_model_ids", provider_id, model_id
                )
                if not isinstance(fallback_model_ids, list) or not all(
                    isinstance(value, str) and value for value in fallback_model_ids
                ):
                    raise _contract_error(
                        "invalid_provider_registry",
                        "request_options/fallback_model_ids have invalid types.",
                        provider_id,
                        model_id,
                    )
                contracts[(provider_id, model_id)] = ModelContract(
                    provider_id=provider_id,
                    model_id=model_id,
                    base_url=base_url,
                    base_url_env=base_url_env,
                    api_key_env=api_key_env,
                    max_n=max_n,
                    context_window=_optional_positive_int(
                        _required(model_payload, "context_window", provider_id, model_id),
                        "context_window",
                        provider_id,
                        model_id,
                    ),
                    max_output_tokens=_optional_positive_int(
                        _required(model_payload, "max_output_tokens", provider_id, model_id),
                        "max_output_tokens",
                        provider_id,
                        model_id,
                    ),
                    json_schema_support=_optional_bool(
                        _required(model_payload, "json_schema_support", provider_id, model_id),
                        "json_schema_support",
                        provider_id,
                        model_id,
                    ),
                    usage_support=_optional_bool(
                        _required(model_payload, "usage_support", provider_id, model_id),
                        "usage_support",
                        provider_id,
                        model_id,
                    ),
                    cache_token_support=_optional_bool(
                        _required(model_payload, "cache_token_support", provider_id, model_id),
                        "cache_token_support",
                        provider_id,
                        model_id,
                    ),
                    timeout_seconds=timeout_seconds,
                    max_retries=max_retries,
                    max_concurrency=max_concurrency,
                    pricing=PricingContract(**pricing_values),
                    request_options=request_options,
                    fallback_model_ids=tuple(fallback_model_ids),
                )

        registry = cls(1, contracts)
        registry._provider_ids = frozenset(declared_providers)
        for contract in contracts.values():
            if len(set(contract.fallback_model_ids)) != len(contract.fallback_model_ids):
                raise _invalid_registry(
                    "Fallback model IDs must be unique.",
                    contract.provider_id,
                    contract.model_id,
                    "fallback_model_ids",
                )
            for fallback_model_id in contract.fallback_model_ids:
                if fallback_model_id == contract.model_id:
                    raise _invalid_registry(
                        "A model cannot fall back to itself.",
                        contract.provider_id,
                        contract.model_id,
                        "fallback_model_ids",
                    )
                if (contract.provider_id, fallback_model_id) not in contracts:
                    raise _invalid_registry(
                        "Fallback model must exist under the same explicit provider.",
                        contract.provider_id,
                        contract.model_id,
                        "fallback_model_ids",
                    )
        return registry

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._provider_ids))

    def model_ids(self, provider_id: str) -> tuple[str, ...]:
        if provider_id not in self._provider_ids:
            raise _contract_error(
                "unknown_provider",
                f"Unknown provider_id '{provider_id}'.",
                provider_id,
            )
        return tuple(
            sorted(
                model_id
                for contract_provider_id, model_id in self._contracts
                if contract_provider_id == provider_id
            )
        )

    def validate_fallback_chain(
        self,
        provider_id: str,
        primary_model_id: str,
        fallback_model_ids: Sequence[str],
    ) -> tuple[str, ...]:
        self.get(provider_id, primary_model_id)
        if isinstance(fallback_model_ids, (str, bytes)) or not isinstance(
            fallback_model_ids,
            SequenceABC,
        ):
            raise _contract_error(
                "invalid_provider_fallbacks",
                "Evaluation fallback models must be an ordered sequence of model IDs.",
                provider_id,
                primary_model_id,
                "fallback_model_ids",
            )

        normalized: list[str] = []
        seen: set[str] = set()
        for fallback_model_id in fallback_model_ids:
            if (
                not isinstance(fallback_model_id, str)
                or _ID_PATTERN.fullmatch(fallback_model_id) is None
            ):
                raise _contract_error(
                    "invalid_provider_fallbacks",
                    "Evaluation fallback models must be registered non-empty model IDs.",
                    provider_id,
                    primary_model_id,
                    "fallback_model_ids",
                )
            if fallback_model_id == primary_model_id or fallback_model_id in seen:
                raise _contract_error(
                    "invalid_provider_fallbacks",
                    "Evaluation fallback models must be unique and cannot include the primary model.",
                    provider_id,
                    primary_model_id,
                    "fallback_model_ids",
                )
            self.get(provider_id, fallback_model_id)
            normalized.append(fallback_model_id)
            seen.add(fallback_model_id)
        return tuple(normalized)

    def environment_names(self) -> frozenset[str]:
        names = set()
        for contract in self._contracts.values():
            names.add(contract.api_key_env)
            names.add(contract.base_url_env)
        return frozenset(names)

    def selection_fingerprint(
        self,
        selections: Sequence[tuple[str, str, str]],
    ) -> str:
        selected: list[dict[str, Any]] = []
        seen_roles: set[str] = set()
        for role, provider_id, model_id in selections:
            if not isinstance(role, str) or not role or role in seen_roles:
                raise ValueError("Provider selection roles must be unique non-empty strings.")
            seen_roles.add(role)
            selected.append(
                {"role": role, "contract": _model_contract_payload(self.get(provider_id, model_id))}
            )
        encoded = json.dumps(
            {"registry_version": self.version, "selections": selected},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def get(self, provider_id: str, model_id: str) -> ModelContract:
        if provider_id not in self._provider_ids:
            raise _contract_error(
                "unknown_provider",
                f"Unknown provider_id '{provider_id}'.",
                provider_id,
                model_id,
            )
        try:
            return self._contracts[(provider_id, model_id)]
        except KeyError as exc:
            raise _contract_error(
                "unknown_model",
                f"Unknown model_id '{model_id}' for provider_id '{provider_id}'.",
                provider_id,
                model_id,
            ) from exc

    def resolve(
        self,
        provider_id: str,
        model_id: str,
        *,
        api_key: str | object = _UNSET,
        base_url: str | object = _UNSET,
        environ: Mapping[str, str] | None = None,
    ) -> ResolvedModelContract:
        contract = self.get(provider_id, model_id)
        environ = os.environ if environ is None else environ
        resolved_key = environ.get(contract.api_key_env) if api_key is _UNSET else api_key
        if not isinstance(resolved_key, str) or not resolved_key.strip():
            raise _contract_error(
                "provider_api_key_missing",
                "The selected provider/model has no configured API key.",
                provider_id,
                model_id,
                contract.api_key_env,
            )

        if base_url is _UNSET:
            resolved_url = environ.get(contract.base_url_env) or contract.base_url
        else:
            resolved_url = base_url
        if not isinstance(resolved_url, str) or not resolved_url.strip():
            raise _contract_error(
                "provider_base_url_missing",
                "The selected provider/model requires an explicit base URL.",
                provider_id,
                model_id,
                contract.base_url_env,
            )
        safe_url = _validate_base_url(resolved_url.strip(), provider_id, model_id)
        return ResolvedModelContract(contract, resolved_key, safe_url)

    def validate_context(
        self,
        provider_id: str,
        model_id: str,
        input_tokens: int,
        requested_output_tokens: int | None = None,
    ) -> None:
        contract = self.get(provider_id, model_id)
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
            raise _contract_error(
                "provider_token_count_invalid",
                "Input token count must be a non-negative integer.",
                provider_id,
                model_id,
            )
        output_tokens = requested_output_tokens or 0
        if contract.max_output_tokens is not None and output_tokens > contract.max_output_tokens:
            raise _contract_error(
                "provider_output_limit_exceeded",
                "Requested output tokens exceed the registered model limit.",
                provider_id,
                model_id,
                "max_output_tokens",
            )
        if contract.context_window is not None and input_tokens + output_tokens > contract.context_window:
            raise _contract_error(
                "provider_context_limit_exceeded",
                "Request tokens exceed the registered model context window.",
                provider_id,
                model_id,
                "context_window",
            )


_registry_lock = threading.Lock()
_default_registry: ProviderRegistry | None = None
_default_registry_source: str | None = None


def get_provider_registry() -> ProviderRegistry:
    global _default_registry, _default_registry_source
    source = os.environ.get(REGISTRY_PATH_ENV) or str(DEFAULT_REGISTRY_PATH)
    with _registry_lock:
        if _default_registry is None or _default_registry_source != source:
            _default_registry = ProviderRegistry.from_file(source)
            _default_registry_source = source
        return _default_registry


_semaphore_lock = threading.Lock()
_semaphores: dict[tuple[str, str, int], threading.BoundedSemaphore] = {}


def _contract_semaphore(contract: ModelContract) -> threading.BoundedSemaphore:
    key = (contract.provider_id, contract.model_id, contract.max_concurrency)
    with _semaphore_lock:
        return _semaphores.setdefault(key, threading.BoundedSemaphore(contract.max_concurrency))


def _ledger_runtime_enabled() -> bool:
    try:
        return bool(_cost_ledger_module().ledger_enabled())
    except ModuleNotFoundError:
        return False


def _cost_ledger_module() -> Any:
    try:
        from codes import cost_ledger as module
    except ModuleNotFoundError:
        import cost_ledger as module
    return module


def _transport_max_retries(contract: ModelContract) -> int:
    return 0 if _ledger_runtime_enabled() else contract.max_retries


def _is_retryable_provider_error(error: BaseException) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True
    name = type(error).__name__.lower()
    return any(marker in name for marker in ("timeout", "ratelimit", "connection"))


class _RegisteredCompletions:
    def __init__(self, resolved: ResolvedModelContract, delegate: Any, registry: ProviderRegistry):
        self._resolved = resolved
        self._delegate = delegate
        self._registry = registry
        self._request_sequence = 0
        self._request_sequence_lock = threading.Lock()

    def _next_request_sequence(self) -> int:
        with self._request_sequence_lock:
            self._request_sequence += 1
            return self._request_sequence

    def create(self, **kwargs: Any) -> Any:
        contract = self._resolved.model
        if kwargs.get("model") != contract.model_id:
            raise _contract_error(
                "provider_model_mismatch",
                "Request model_id does not match the registered client contract.",
                contract.provider_id,
                contract.model_id,
                "model",
            )
        n = kwargs.get("n", 1)
        if isinstance(n, bool) or not isinstance(n, int) or n < 1 or n > contract.max_n:
            raise _contract_error(
                "provider_max_n_exceeded",
                "Request choice count exceeds the registered max_n.",
                contract.provider_id,
                contract.model_id,
                "n",
            )
        response_format = kwargs.get("response_format")
        if isinstance(response_format, Mapping) and response_format.get("type") == "json_schema" and contract.json_schema_support is not True:
            raise _contract_error(
                "provider_json_schema_unsupported",
                "JSON Schema requests are not enabled for this provider/model contract.",
                contract.provider_id,
                contract.model_id,
                "json_schema_support",
            )
        input_tokens = kwargs.pop("_input_token_count", None)
        requested_output = kwargs.get("max_completion_tokens", kwargs.get("max_tokens"))
        if requested_output is not None:
            if isinstance(requested_output, bool) or not isinstance(requested_output, int) or requested_output <= 0:
                raise _contract_error(
                    "provider_output_limit_invalid",
                    "Requested output token limit must be a positive integer.",
                    contract.provider_id,
                    contract.model_id,
                )
        if input_tokens is not None:
            self._registry.validate_context(
                contract.provider_id,
                contract.model_id,
                input_tokens,
                requested_output,
            )
        elif contract.max_output_tokens is not None and requested_output is not None and requested_output > contract.max_output_tokens:
            raise _contract_error(
                "provider_output_limit_exceeded",
                "Requested output tokens exceed the registered model limit.",
                contract.provider_id,
                contract.model_id,
                "max_output_tokens",
            )
        logical_call_id = kwargs.pop("_paper2code_logical_call_id", None)
        fixed_attempt_id = kwargs.pop("_paper2code_attempt_id", None)
        request_sequence = kwargs.pop("_paper2code_request_sequence", None)
        fallback_sequence = kwargs.pop("_paper2code_fallback_sequence", None)
        if request_sequence is None:
            request_sequence = self._next_request_sequence()
        if logical_call_id is None:
            logical_call_id = _cost_ledger_module().new_logical_call_id()
        ledger_request = dict(kwargs)
        if input_tokens is not None:
            ledger_request["_input_token_count"] = input_tokens
        semaphore = _contract_semaphore(contract)
        with semaphore:
            if not _ledger_runtime_enabled():
                return self._delegate.create(**kwargs)
            cost_ledger = _cost_ledger_module()

            retry_sequence = 0
            while True:
                if fixed_attempt_id is not None and retry_sequence == 0:
                    attempt_id = fixed_attempt_id
                elif fixed_attempt_id is not None:
                    attempt_id = f"{fixed_attempt_id}-retry-{retry_sequence}"
                else:
                    attempt_id = cost_ledger.new_attempt_id()
                cost_ledger.reserve_call_from_env(
                    registry_version=self._registry.version,
                    contract=contract,
                    request=ledger_request,
                    logical_call_id=logical_call_id,
                    attempt_id=attempt_id,
                    request_sequence=request_sequence,
                    retry_sequence=retry_sequence,
                    fallback_sequence=fallback_sequence,
                )
                try:
                    response = self._delegate.create(**kwargs)
                except BaseException as exc:
                    status = cost_ledger.cancellation_status(exc)
                    cost_ledger.record_call_status_from_env(
                        registry_version=self._registry.version,
                        contract=contract,
                        logical_call_id=logical_call_id,
                        attempt_id=attempt_id,
                        request_sequence=request_sequence,
                        retry_sequence=retry_sequence,
                        status=status,
                        error=exc,
                        fallback_sequence=fallback_sequence,
                    )
                    if (
                        status == "failed"
                        and retry_sequence < contract.max_retries
                        and _is_retryable_provider_error(exc)
                    ):
                        retry_sequence += 1
                        continue
                    raise
                cost_ledger.record_call_status_from_env(
                    registry_version=self._registry.version,
                    contract=contract,
                    logical_call_id=logical_call_id,
                    attempt_id=attempt_id,
                    request_sequence=request_sequence,
                    retry_sequence=retry_sequence,
                    status="completed",
                    response=response,
                    fallback_sequence=fallback_sequence,
                )
                return response


class RegisteredOpenAIClient:
    def __init__(self, resolved: ResolvedModelContract, delegate: Any, registry: ProviderRegistry):
        self.contract = resolved.model
        completions = _RegisteredCompletions(resolved, delegate.chat.completions, registry)
        self.chat = type("RegisteredChat", (), {"completions": completions})()

    def __repr__(self) -> str:
        return (
            "RegisteredOpenAIClient("
            f"provider_id={self.contract.provider_id!r}, model_id={self.contract.model_id!r})"
        )


def create_registered_client(
    provider_id: str,
    model_id: str,
    *,
    registry: ProviderRegistry | None = None,
    api_key: str | object = _UNSET,
    base_url: str | object = _UNSET,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> RegisteredOpenAIClient:
    registry = registry or get_provider_registry()
    resolved = registry.resolve(
        provider_id,
        model_id,
        api_key=api_key,
        base_url=base_url,
        environ=environ,
    )
    if client_factory is None:
        from openai import OpenAI

        client_factory = OpenAI
    delegate = client_factory(
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        timeout=resolved.model.timeout_seconds,
        max_retries=_transport_max_retries(resolved.model),
    )
    return RegisteredOpenAIClient(resolved, delegate, registry)
