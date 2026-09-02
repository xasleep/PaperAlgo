from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

CODES_DIR = Path(__file__).resolve().parents[1] / "codes"
if str(CODES_DIR) not in sys.path:
    sys.path.insert(0, str(CODES_DIR))

from codes import eval as eval_module
from codes.cost_ledger import (
    CostBudgetExceededError,
    CostBudgetUnknownError,
    ledger_fallback_sequence,
    summarize_job_costs,
)
from codes.provider_registry import ProviderRegistry, create_registered_client
from web_api import job_service, main as main_module
from web_api.database import connect_database, initialize_database, normalize_database_path
from web_api.job_repository import JobRepository
from web_api.schemas import WebSettings


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"


def _model_payload(
    model_id: str,
    *,
    pricing_status: str = "configured",
    currency: str | None = "USD",
    input_price: float | None = 0.15,
    cached_input_price: float | None = 0.03,
    output_price: float | None = 0.60,
    max_n: int = 1,
    max_retries: int = 0,
    max_output_tokens: int | None = 16,
    cache_support: bool | None = True,
    fallback_model_ids: list[str] | None = None,
) -> dict[str, Any]:
    if pricing_status == "unknown":
        pricing = {
            "status": "unknown",
            "currency": None,
            "input_per_million": None,
            "cached_input_per_million": None,
            "output_per_million": None,
            "effective_date": None,
        }
    else:
        pricing = {
            "status": "configured",
            "currency": currency,
            "input_per_million": input_price,
            "cached_input_per_million": cached_input_price,
            "output_per_million": output_price,
            "effective_date": "2026-08-31",
        }
    return {
        "base_url": None,
        "base_url_env": "FAKE_BASE_URL",
        "api_key_env": "FAKE_API_KEY",
        "max_n": max_n,
        "context_window": 100000,
        "max_output_tokens": max_output_tokens,
        "json_schema_support": True,
        "usage_support": True,
        "cache_token_support": cache_support,
        "timeout_seconds": 2,
        "max_retries": max_retries,
        "max_concurrency": 8,
        "pricing": pricing,
        "request_options": {"temperature": 0},
        "fallback_model_ids": fallback_model_ids or [],
    }


def _registry(
    *,
    models: dict[str, dict[str, Any]] | None = None,
    pricing_status: str = "configured",
    currency: str | None = "USD",
    input_price: float | None = 0.15,
    cached_input_price: float | None = 0.03,
    output_price: float | None = 0.60,
    max_n: int = 1,
    max_retries: int = 0,
    max_output_tokens: int | None = 16,
    cache_support: bool | None = True,
) -> ProviderRegistry:
    payload_models = models or {
        "fake-chat": _model_payload(
            "fake-chat",
            pricing_status=pricing_status,
            currency=currency,
            input_price=input_price,
            cached_input_price=cached_input_price,
            output_price=output_price,
            max_n=max_n,
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            cache_support=cache_support,
        )
    }
    return ProviderRegistry.from_mapping(
        {"version": 1, "providers": {"fake": {"models": payload_models}}}
    )


def _job_request(**overrides: object) -> dict[str, object]:
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


def _create_job(
    db_path: Path,
    job_id: str = "ledger_job",
    *,
    budget_policy: str = "none",
    budget_currency: str | None = None,
    budget_amount: str | None = None,
) -> JobRepository:
    repository = JobRepository(db_path)
    request = _job_request(
        cost_budget_policy=budget_policy,
        cost_budget_currency=budget_currency,
        cost_budget_amount=budget_amount,
    )
    repository.create_job(job_id=job_id, request=request, paper_name="paper")
    return repository


def _enable_ledger(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    *,
    job_id: str = "ledger_job",
    stage: str = "evaluation",
    stage_attempt: int = 1,
    recovery_attempt: int = 0,
    repair_attempt: int | None = None,
) -> None:
    monkeypatch.setenv("PAPER2CODE_COST_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("PAPER2CODE_COST_JOB_ID", job_id)
    monkeypatch.setenv("PAPER2CODE_COST_STAGE", stage)
    monkeypatch.setenv("PAPER2CODE_COST_STAGE_ATTEMPT", str(stage_attempt))
    monkeypatch.setenv("PAPER2CODE_COST_RECOVERY_ATTEMPT", str(recovery_attempt))
    monkeypatch.delenv("PAPER2CODE_COST_FALLBACK_SEQUENCE", raising=False)
    if repair_attempt is None:
        monkeypatch.delenv("PAPER2CODE_COST_REPAIR_ATTEMPT", raising=False)
    else:
        monkeypatch.setenv("PAPER2CODE_COST_REPAIR_ATTEMPT", str(repair_attempt))


class _FakeResponse(dict):
    def model_dump_json(self) -> str:
        return json.dumps(self)


class _ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        usage: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.usage = usage


class _CancelledError(Exception):
    def __init__(self, usage: dict[str, Any] | None = None) -> None:
        super().__init__("request cancelled")
        self.usage = usage


class _FakeCompletions:
    def __init__(self, outcomes: list[Any] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.outcomes = outcomes or [
            _FakeResponse(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "prompt_tokens_details": {"cached_tokens": 20},
                        "completion_tokens_details": {"reasoning_tokens": 4},
                    },
                }
            )
        ]

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, completions: _FakeCompletions | None = None) -> None:
        self.completions = completions or _FakeCompletions()
        self.chat = type("Chat", (), {"completions": self.completions})()


def _client(
    registry: ProviderRegistry,
    fake_client: _FakeClient,
    *,
    model_id: str = "fake-chat",
) -> tuple[Any, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> _FakeClient:
        captured.update(kwargs)
        return fake_client

    client = create_registered_client(
        "fake",
        model_id,
        registry=registry,
        environ={"FAKE_API_KEY": "secret-value", "FAKE_BASE_URL": "https://fake.invalid/v1"},
        client_factory=factory,
    )
    return client, captured


def _completed_rows(db_path: Path, job_id: str = "ledger_job") -> list[dict[str, Any]]:
    with closing(connect_database(db_path)) as connection:
        rows = connection.execute(
            """
            SELECT * FROM remote_call_ledger
            WHERE job_id = ? AND status IN ('completed', 'failed', 'cancelled')
            ORDER BY id
            """,
            (job_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_known_pricing_records_exact_decimal_and_keeps_sensitive_payload_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    _create_job(db_path)
    _enable_ledger(monkeypatch, db_path)
    registry = _registry()
    fake = _FakeClient()
    client, captured = _client(registry, fake)

    client.chat.completions.create(
        model="fake-chat",
        messages=[{"role": "user", "content": "top secret prompt"}],
        n=1,
        max_tokens=10,
        _input_token_count=100,
    )

    rows = _completed_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["job_id"] == "ledger_job"
    assert row["stage"] == "evaluation"
    assert row["stage_attempt"] == 1
    assert row["recovery_attempt"] == 0
    assert row["repair_attempt"] is None
    assert row["provider_id"] == "fake"
    assert row["model_id"] == "fake-chat"
    assert row["request_sequence"] == 1
    assert row["retry_sequence"] == 0
    assert row["fallback_sequence"] == 0
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 10
    assert row["cached_input_tokens"] == 20
    assert row["reasoning_tokens"] == 4
    assert row["pricing_contract_version"] == "1"
    assert len(row["pricing_contract_fingerprint"]) == 64
    assert row["currency"] == "USD"
    assert row["cost_status"] == "actual"
    assert row["cost_amount"] == "0.0000186"
    assert fake.completions.calls == [
        {
            "model": "fake-chat",
            "messages": [{"role": "user", "content": "top secret prompt"}],
            "n": 1,
            "max_tokens": 10,
        }
    ]
    assert captured["api_key"] == "secret-value"
    assert captured["max_retries"] == 0

    with closing(connect_database(db_path)) as connection:
        schema = "\n".join(
            row["name"] for row in connection.execute("PRAGMA table_info(remote_call_ledger)")
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE remote_call_ledger SET cost_amount = ? WHERE id = ?",
                ("0", row["id"]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM remote_call_ledger WHERE id = ?", (row["id"],))
    for forbidden in (
        "api_key",
        "authorization",
        "credential",
        "prompt",
        "messages",
        "response",
        "path",
    ):
        assert forbidden not in schema.lower()
    database_bytes = b"".join(path.read_bytes() for path in tmp_path.glob("paper2code.db*"))
    assert b"top secret prompt" not in database_bytes
    assert b"secret-value" not in database_bytes


def test_unknown_pricing_missing_usage_and_partial_usage_are_not_zero_filled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    _create_job(db_path)
    _enable_ledger(monkeypatch, db_path)

    unknown_client, _ = _client(
        _registry(pricing_status="unknown"),
        _FakeClient(
            _FakeCompletions(
                [
                    _FakeResponse(
                        {
                            "choices": [{"message": {"content": "ok"}}],
                            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                        }
                    )
                ]
            )
        ),
    )
    unknown_client.chat.completions.create(
        model="fake-chat", messages=[], n=1, max_tokens=3, _input_token_count=7
    )

    no_usage_client, _ = _client(
        _registry(cached_input_price=None, cache_support=False),
        _FakeClient(_FakeCompletions([_FakeResponse({"choices": [{"message": {"content": "ok"}}]})])),
    )
    no_usage_client.chat.completions.create(
        model="fake-chat", messages=[], n=1, max_tokens=3, _input_token_count=7
    )

    partial_client, _ = _client(
        _registry(cached_input_price=None, cache_support=False),
        _FakeClient(
            _FakeCompletions(
                [
                    _FakeResponse(
                        {
                            "choices": [{"message": {"content": "ok"}}],
                            "usage": {"prompt_tokens": 5},
                        }
                    )
                ]
            )
        ),
    )
    partial_client.chat.completions.create(
        model="fake-chat", messages=[], n=1, max_tokens=3, _input_token_count=5
    )

    rows = _completed_rows(db_path)
    assert [row["cost_status"] for row in rows] == ["unknown", "unknown", "estimated"]
    assert rows[0]["cost_amount"] is None
    assert rows[0]["input_tokens"] == 7
    assert rows[0]["output_tokens"] == 3
    assert rows[1]["input_tokens"] is None
    assert rows[1]["output_tokens"] is None
    assert rows[1]["cost_amount"] is None
    assert rows[2]["input_tokens"] == 5
    assert rows[2]["output_tokens"] is None
    assert rows[2]["cost_amount"] == "0.00000075"
    summary = summarize_job_costs(db_path, "ledger_job")
    assert summary["unknown_attempts"] == 2
    assert summary["estimated_by_currency"] == {"USD": "0.00000075"}


def test_max_n_one_evaluation_split_records_each_transport_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    _create_job(db_path)
    _enable_ledger(monkeypatch, db_path)
    registry = _registry(
        max_n=1,
        cached_input_price=None,
        cache_support=False,
        max_output_tokens=4,
    )
    fake = _FakeClient(
        _FakeCompletions(
            [
                _FakeResponse(
                    {
                        "choices": [{"message": {"content": f"ok-{index}"}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 1},
                    }
                )
                for index in range(3)
            ]
        )
    )
    client, _ = _client(registry, fake)
    monkeypatch.setattr(eval_module, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(eval_module, "client", client)

    _, completion_json, generated_n = eval_module.run_completion_requests(
        "fake",
        "fake-chat",
        [{"role": "user", "content": "evaluate"}],
        3,
        input_tokens=10,
    )

    assert generated_n == 3
    assert len(completion_json["choices"]) == 3
    rows = _completed_rows(db_path)
    assert [row["request_sequence"] for row in rows] == [1, 2, 3]
    assert [row["cost_status"] for row in rows] == ["actual", "actual", "actual"]
    assert summarize_job_costs(db_path, "ledger_job")["actual_by_currency"] == {
        "USD": "0.0000063"
    }


def test_retry_and_fallback_sequences_are_recorded_as_separate_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    _create_job(db_path)
    _enable_ledger(monkeypatch, db_path)
    registry = _registry(max_retries=1, cached_input_price=None, cache_support=False)
    fake = _FakeClient(
        _FakeCompletions(
            [
                _ProviderError(
                    "rate limit",
                    status_code=429,
                    usage={"prompt_tokens": 2},
                ),
                _FakeResponse(
                    {
                        "choices": [{"message": {"content": "retry ok"}}],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                    }
                ),
            ]
        )
    )
    client, captured = _client(registry, fake)

    client.chat.completions.create(
        model="fake-chat", messages=[], n=1, max_tokens=1, _input_token_count=2
    )

    fallback_client, _ = _client(
        _registry(cached_input_price=None, cache_support=False),
        _FakeClient(
            _FakeCompletions(
                [
                    _FakeResponse(
                        {
                            "choices": [{"message": {"content": "fallback ok"}}],
                            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                        }
                    )
                ]
            )
        ),
    )
    with ledger_fallback_sequence(1):
        fallback_client.chat.completions.create(
            model="fake-chat", messages=[], n=1, max_tokens=1, _input_token_count=2
        )

    assert captured["max_retries"] == 0
    rows = _completed_rows(db_path)
    assert [(row["status"], row["retry_sequence"]) for row in rows[:2]] == [
        ("failed", 0),
        ("completed", 1),
    ]
    assert rows[2]["status"] == "completed"
    assert rows[2]["fallback_sequence"] == 1
    assert len({row["attempt_id"] for row in rows}) == 3


def test_recovery_duplicate_calls_are_counted_and_attempt_id_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    _create_job(db_path)
    registry = _registry(cached_input_price=None, cache_support=False)

    _enable_ledger(monkeypatch, db_path, stage_attempt=1, recovery_attempt=0)
    first_client, _ = _client(
        registry,
        _FakeClient(
            _FakeCompletions(
                [
                    _FakeResponse(
                        {
                            "choices": [{"message": {"content": "first"}}],
                            "usage": {"prompt_tokens": 4, "completion_tokens": 1},
                        }
                    )
                ]
            )
        ),
    )
    first_client.chat.completions.create(
        model="fake-chat", messages=[], n=1, max_tokens=1, _input_token_count=4
    )

    _enable_ledger(monkeypatch, db_path, stage_attempt=2, recovery_attempt=1)
    recovered_client, _ = _client(
        registry,
        _FakeClient(
            _FakeCompletions(
                [
                    _FakeResponse(
                        {
                            "choices": [{"message": {"content": "recovered"}}],
                            "usage": {"prompt_tokens": 4, "completion_tokens": 1},
                        }
                    )
                ]
            )
        ),
    )
    recovered_client.chat.completions.create(
        model="fake-chat", messages=[], n=1, max_tokens=1, _input_token_count=4
    )

    fixed_client, _ = _client(
        registry,
        _FakeClient(
            _FakeCompletions(
                [
                    _FakeResponse(
                        {
                            "choices": [{"message": {"content": "fixed"}}],
                            "usage": {"prompt_tokens": 4, "completion_tokens": 1},
                        }
                    )
                ]
            )
        ),
    )
    for _ in range(2):
        fixed_client.chat.completions.create(
            model="fake-chat",
            messages=[],
            n=1,
            max_tokens=1,
            _input_token_count=4,
            _paper2code_logical_call_id="fixed-logical",
            _paper2code_attempt_id="fixed-attempt",
        )

    rows = _completed_rows(db_path)
    assert [(row["stage_attempt"], row["recovery_attempt"]) for row in rows[:2]] == [
        (1, 0),
        (2, 1),
    ]
    assert [row["attempt_id"] for row in rows].count("fixed-attempt") == 1
    assert summarize_job_costs(db_path, "ledger_job")["actual_by_currency"] == {
        "USD": "0.0000036"
    }


def test_multi_currency_summaries_are_not_converted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    _create_job(db_path)
    _enable_ledger(monkeypatch, db_path)
    registry = _registry(
        models={
            "usd-chat": _model_payload(
                "usd-chat",
                input_price=1.0,
                cached_input_price=None,
                output_price=1.0,
                cache_support=False,
            ),
            "eur-chat": _model_payload(
                "eur-chat",
                currency="EUR",
                input_price=2.0,
                cached_input_price=None,
                output_price=2.0,
                cache_support=False,
            ),
        }
    )
    for model_id in ("usd-chat", "eur-chat"):
        client, _ = _client(
            registry,
            _FakeClient(
                _FakeCompletions(
                    [
                        _FakeResponse(
                            {
                                "choices": [{"message": {"content": model_id}}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                            }
                        )
                    ]
                )
            ),
            model_id=model_id,
        )
        client.chat.completions.create(
            model=model_id, messages=[], n=1, max_tokens=1, _input_token_count=1
        )

    summary = summarize_job_costs(db_path, "ledger_job")
    assert summary["actual_by_currency"] == {"EUR": "0.000004", "USD": "0.000002"}
    assert "actual_total" not in summary


def test_hard_budget_boundary_exceed_and_concurrent_competition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    _create_job(
        db_path,
        budget_policy="hard",
        budget_currency="USD",
        budget_amount="0.000002",
    )
    _enable_ledger(monkeypatch, db_path)
    registry = _registry(
        input_price=1.0,
        cached_input_price=None,
        output_price=1.0,
        cache_support=False,
        max_output_tokens=1,
    )
    fake = _FakeClient(
        _FakeCompletions(
            [
                _FakeResponse(
                    {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                )
            ]
        )
    )
    client, _ = _client(registry, fake)

    client.chat.completions.create(
        model="fake-chat", messages=[], n=1, max_tokens=1, _input_token_count=1
    )
    with pytest.raises(CostBudgetExceededError):
        client.chat.completions.create(
            model="fake-chat", messages=[], n=1, max_tokens=1, _input_token_count=1
        )
    assert len(fake.completions.calls) == 1

    _create_job(
        db_path,
        job_id="concurrent_budget",
        budget_policy="hard",
        budget_currency="USD",
        budget_amount="0.000002",
    )
    _enable_ledger(monkeypatch, db_path, job_id="concurrent_budget")

    def call_once(_: int) -> str:
        thread_client, _ = _client(
            registry,
            _FakeClient(
                _FakeCompletions(
                    [
                        _FakeResponse(
                            {
                                "choices": [{"message": {"content": "ok"}}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                            }
                        )
                    ]
                )
            ),
        )
        try:
            thread_client.chat.completions.create(
                model="fake-chat",
                messages=[],
                n=1,
                max_tokens=1,
                _input_token_count=1,
            )
            return "ok"
        except CostBudgetExceededError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(call_once, range(2)))
    assert outcomes == ["blocked", "ok"]
    assert summarize_job_costs(db_path, "concurrent_budget")["actual_by_currency"] == {
        "USD": "0.000002"
    }


def test_hard_budget_fails_closed_when_pricing_or_bound_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    _create_job(
        db_path,
        budget_policy="hard",
        budget_currency="USD",
        budget_amount="1",
    )
    _enable_ledger(monkeypatch, db_path)

    unknown_fake = _FakeClient()
    unknown_client, _ = _client(_registry(pricing_status="unknown"), unknown_fake)
    with pytest.raises(CostBudgetUnknownError):
        unknown_client.chat.completions.create(
            model="fake-chat", messages=[], n=1, max_tokens=1, _input_token_count=1
        )
    assert unknown_fake.completions.calls == []

    unbounded_fake = _FakeClient()
    unbounded_client, _ = _client(
        _registry(cached_input_price=None, cache_support=False, max_output_tokens=None),
        unbounded_fake,
    )
    with pytest.raises(CostBudgetUnknownError):
        unbounded_client.chat.completions.create(
            model="fake-chat", messages=[], n=1, _input_token_count=1
        )
    assert unbounded_fake.completions.calls == []


def test_failed_cancelled_and_timeout_attempts_keep_partial_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper2code.db"
    _create_job(db_path)
    _enable_ledger(monkeypatch, db_path)
    registry = _registry(cached_input_price=None, cache_support=False)
    outcomes = [
        _ProviderError(
            "provider failed",
            status_code=500,
            usage={"prompt_tokens": 4, "completion_tokens": 2},
        ),
        _ProviderError(
            "timeout",
            status_code=408,
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        ),
        _CancelledError(usage={"prompt_tokens": 2, "completion_tokens": 1}),
    ]

    for outcome in outcomes:
        client, _ = _client(registry, _FakeClient(_FakeCompletions([outcome])))
        with pytest.raises(type(outcome)):
            client.chat.completions.create(
                model="fake-chat", messages=[], n=1, max_tokens=2, _input_token_count=4
            )

    rows = _completed_rows(db_path)
    assert [row["status"] for row in rows] == ["failed", "failed", "cancelled"]
    assert [row["cost_status"] for row in rows] == ["actual", "actual", "actual"]
    assert [row["cost_amount"] for row in rows] == [
        "0.0000018",
        "0.00000075",
        "0.0000009",
    ]


def _authorize(client: TestClient) -> dict[str, str]:
    session = client.get(f"{API_PREFIX}/session", headers={"Origin": LOCAL_ORIGIN})
    assert session.status_code == 200
    return {
        "Origin": LOCAL_ORIGIN,
        "X-CSRF-Token": session.json()["csrf_token"],
    }


def _settings(reproduce_secret: str, evaluation_secret: str) -> WebSettings:
    return WebSettings(
        reproduce={
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "api_key": reproduce_secret,
            "base_url": "https://reproduce.invalid/v1",
        },
        evaluation={
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "api_key": evaluation_secret,
            "base_url": "https://evaluation.invalid/v1",
            "fallback_models": [],
        },
    )


def test_api_returns_sanitized_cost_summary_and_accepts_budget_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / ".local" / "paper2code.db"
    uploads_dir = tmp_path / ".local" / "uploads"
    runs_dir = tmp_path / "runs"
    main_module._SQLITE_REPOSITORIES.clear()
    monkeypatch.setenv("JOB_RUNTIME", "sqlite")
    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(db_path))
    monkeypatch.setattr(job_service, "UPLOADS_DIR", uploads_dir)
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(
        main_module,
        "load_settings",
        lambda: _settings("REPRODUCE-SECRET", "EVALUATION-SECRET"),
    )

    with TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        headers = _authorize(client)
        upload = client.post(
            f"{API_PREFIX}/uploads",
            headers=headers,
            files={"file": ("paper.pdf", b"%PDF-test", "application/pdf")},
        )
        assert upload.status_code == 200
        created = client.post(
            f"{API_PREFIX}/jobs",
            headers=headers,
            json={
                "upload_id": upload.json()["upload_id"],
                "paper_name": "paper",
                "domain": "statistics",
                "eval_type": "ref_free",
                "generated_n": 1,
                "auto_refine": False,
                "max_repair_rounds": 0,
                "console_output": "quiet",
                "skip_mineru": False,
                "pdf_markdown_path": "",
                "cost_budget_policy": "hard",
                "cost_budget_currency": "USD",
                "cost_budget_amount": "1.00",
            },
        )
        assert created.status_code == 200
        job_id = created.json()["job_id"]
        _enable_ledger(monkeypatch, db_path, job_id=job_id)
        ledger_client, _ = _client(
            _registry(cached_input_price=None, cache_support=False),
            _FakeClient(
                _FakeCompletions(
                    [
                        _FakeResponse(
                            {
                                "choices": [{"message": {"content": "ok"}}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                            }
                        )
                    ]
                )
            ),
        )
        ledger_client.chat.completions.create(
            model="fake-chat",
            messages=[{"role": "user", "content": "do not expose"}],
            n=1,
            max_tokens=1,
            _input_token_count=1,
        )
        detail = client.get(f"{API_PREFIX}/jobs/{job_id}", headers={"Origin": LOCAL_ORIGIN})

    assert detail.status_code == 200
    summary = detail.json()["cost_summary"]
    assert summary["budget_policy"] == "hard"
    assert summary["budget_currency"] == "USD"
    assert summary["budget_amount"] == "1.00"
    assert summary["actual_by_currency"] == {"USD": "0.00000075"}
    serialized_summary = json.dumps(summary, sort_keys=True).lower()
    for forbidden in (
        "attempt_id",
        "logical_call_id",
        "prompt",
        "credential",
        "api_key",
        "secret",
        "authorization",
        "do not expose",
        str(tmp_path).lower(),
    ):
        assert forbidden not in serialized_summary


def test_migration_8_reruns_from_pr06a_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from web_api import database as database_module

    db_path = tmp_path / "version-7" / "paper2code.db"
    current_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations[:7])
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
                    "historical_pr06a",
                    "0" * 64,
                    "paper",
                    "0" * 32,
                    "statistics",
                    "ref_free",
                    1,
                    0,
                    0,
                    "quiet",
                    0,
                    "",
                    "completed",
                    "completed",
                    "accepted",
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
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        historical = connection.execute(
            "SELECT * FROM jobs WHERE job_id = 'historical_pr06a'"
        ).fetchone()

    assert versions == list(range(1, 9))
    assert "remote_call_ledger" in tables
    assert {
        "cost_budget_policy",
        "cost_budget_currency",
        "cost_budget_amount",
    }.issubset(columns)
    assert historical["cost_budget_policy"] == "none"
    assert historical["cost_budget_currency"] is None
    assert historical["cost_budget_amount"] is None
    assert historical["evaluation_status"] == "completed"
