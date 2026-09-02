"""Append-only remote-call cost ledger and hard-budget guardrails."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any


LEDGER_DB_PATH_ENV = "PAPER2CODE_COST_LEDGER_DB_PATH"
JOB_ID_ENV = "PAPER2CODE_COST_JOB_ID"
STAGE_ENV = "PAPER2CODE_COST_STAGE"
STAGE_ATTEMPT_ENV = "PAPER2CODE_COST_STAGE_ATTEMPT"
REPAIR_ATTEMPT_ENV = "PAPER2CODE_COST_REPAIR_ATTEMPT"
RECOVERY_ATTEMPT_ENV = "PAPER2CODE_COST_RECOVERY_ATTEMPT"
FALLBACK_SEQUENCE_ENV = "PAPER2CODE_COST_FALLBACK_SEQUENCE"

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
MILLION = Decimal("1000000")
SQLITE_BUSY_TIMEOUT_MS = 5000


class CostLedgerError(RuntimeError):
    """Base class for sanitized cost ledger failures."""

    code = "cost_ledger_error"


class CostBudgetExceededError(CostLedgerError):
    code = "cost_budget_exceeded"


class CostBudgetUnknownError(CostLedgerError):
    code = "cost_budget_unknown"


@dataclass(frozen=True)
class CostContext:
    db_path: Path
    job_id: str
    stage: str
    stage_attempt: int
    repair_attempt: int | None
    recovery_attempt: int
    fallback_sequence: int


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    present: bool = False


@dataclass(frozen=True)
class CostAmount:
    status: str
    currency: str | None
    amount: Decimal | None
    pricing_status: str
    pricing_contract_version: str
    pricing_contract_fingerprint: str


@contextmanager
def ledger_fallback_sequence(sequence: int) -> Iterator[None]:
    previous = os.environ.get(FALLBACK_SEQUENCE_ENV)
    os.environ[FALLBACK_SEQUENCE_ENV] = str(_nonnegative_int(sequence, "sequence"))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(FALLBACK_SEQUENCE_ENV, None)
        else:
            os.environ[FALLBACK_SEQUENCE_ENV] = previous


def ledger_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(
        env.get(LEDGER_DB_PATH_ENV)
        and env.get(JOB_ID_ENV)
        and env.get(STAGE_ENV)
        and env.get(STAGE_ATTEMPT_ENV)
    )


def context_from_env(environ: Mapping[str, str] | None = None) -> CostContext | None:
    env = os.environ if environ is None else environ
    if not ledger_enabled(env):
        return None
    return CostContext(
        db_path=Path(str(env[LEDGER_DB_PATH_ENV])),
        job_id=_bounded_text(env[JOB_ID_ENV], "job_id"),
        stage=_bounded_text(env[STAGE_ENV], "stage"),
        stage_attempt=_positive_int(env[STAGE_ATTEMPT_ENV], "stage_attempt"),
        repair_attempt=(
            None
            if not env.get(REPAIR_ATTEMPT_ENV)
            else _positive_int(env[REPAIR_ATTEMPT_ENV], "repair_attempt")
        ),
        recovery_attempt=_nonnegative_int(
            env.get(RECOVERY_ATTEMPT_ENV, "0"),
            "recovery_attempt",
        ),
        fallback_sequence=_nonnegative_int(
            env.get(FALLBACK_SEQUENCE_ENV, "0"),
            "fallback_sequence",
        ),
    )


def reserve_call_from_env(
    *,
    registry_version: int,
    contract: Any,
    request: Mapping[str, Any],
    logical_call_id: str,
    attempt_id: str,
    request_sequence: int,
    retry_sequence: int,
    fallback_sequence: int | None = None,
) -> bool:
    context = context_from_env()
    if context is None:
        return False
    if fallback_sequence is not None:
        context = CostContext(
            db_path=context.db_path,
            job_id=context.job_id,
            stage=context.stage,
            stage_attempt=context.stage_attempt,
            repair_attempt=context.repair_attempt,
            recovery_attempt=context.recovery_attempt,
            fallback_sequence=_nonnegative_int(fallback_sequence, "fallback_sequence"),
        )
    reserve_call(
        context=context,
        registry_version=registry_version,
        contract=contract,
        request=request,
        logical_call_id=logical_call_id,
        attempt_id=attempt_id,
        request_sequence=request_sequence,
        retry_sequence=retry_sequence,
    )
    return True


def record_call_status_from_env(
    *,
    registry_version: int,
    contract: Any,
    logical_call_id: str,
    attempt_id: str,
    request_sequence: int,
    retry_sequence: int,
    status: str,
    response: Any | None = None,
    error: BaseException | None = None,
    fallback_sequence: int | None = None,
) -> bool:
    context = context_from_env()
    if context is None:
        return False
    if fallback_sequence is not None:
        context = CostContext(
            db_path=context.db_path,
            job_id=context.job_id,
            stage=context.stage,
            stage_attempt=context.stage_attempt,
            repair_attempt=context.repair_attempt,
            recovery_attempt=context.recovery_attempt,
            fallback_sequence=_nonnegative_int(fallback_sequence, "fallback_sequence"),
        )
    record_call_status(
        context=context,
        registry_version=registry_version,
        contract=contract,
        logical_call_id=logical_call_id,
        attempt_id=attempt_id,
        request_sequence=request_sequence,
        retry_sequence=retry_sequence,
        status=status,
        response=response,
        error=error,
    )
    return True


def reserve_call(
    *,
    context: CostContext,
    registry_version: int,
    contract: Any,
    request: Mapping[str, Any],
    logical_call_id: str,
    attempt_id: str,
    request_sequence: int,
    retry_sequence: int,
) -> None:
    logical_call_id = _bounded_text(logical_call_id, "logical_call_id")
    attempt_id = _bounded_text(attempt_id, "attempt_id")
    request_sequence = _positive_int(request_sequence, "request_sequence")
    retry_sequence = _nonnegative_int(retry_sequence, "retry_sequence")
    pricing = _pricing_metadata(registry_version, contract)
    upper_bound = _estimated_upper_bound(pricing, contract, request)
    connection = _connect(context.db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if _attempt_row_exists(connection, attempt_id):
                connection.commit()
                return
            job = connection.execute(
                """
                SELECT cost_budget_policy, cost_budget_currency, cost_budget_amount
                FROM jobs
                WHERE job_id = ?
                """,
                (context.job_id,),
            ).fetchone()
            if job is None:
                raise CostLedgerError("Cost ledger job context is not available.")
            policy = str(job["cost_budget_policy"] or "none")
            if policy == "hard":
                _assert_hard_budget_allows(
                    connection,
                    context.job_id,
                    str(job["cost_budget_currency"] or ""),
                    str(job["cost_budget_amount"] or ""),
                    upper_bound,
                )
            now = _utc_now()
            connection.execute(
                """
                INSERT OR IGNORE INTO remote_call_ledger (
                    job_id, logical_call_id, attempt_id, stage, stage_attempt,
                    repair_attempt, recovery_attempt, provider_id, model_id,
                    request_sequence, retry_sequence, fallback_sequence,
                    pricing_contract_version, pricing_contract_fingerprint,
                    pricing_status, currency, cost_status, cost_amount,
                    input_tokens, output_tokens, cached_input_tokens,
                    reasoning_tokens, total_tokens, status, started_at,
                    completed_at, event_time, error_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?, NULL, ?, NULL, ?)
                """,
                (
                    context.job_id,
                    logical_call_id,
                    attempt_id,
                    context.stage,
                    context.stage_attempt,
                    context.repair_attempt,
                    context.recovery_attempt,
                    _bounded_text(contract.provider_id, "provider_id"),
                    _bounded_text(contract.model_id, "model_id"),
                    request_sequence,
                    retry_sequence,
                    context.fallback_sequence,
                    pricing.pricing_contract_version,
                    pricing.pricing_contract_fingerprint,
                    pricing.pricing_status,
                    upper_bound.currency if upper_bound else pricing.currency,
                    upper_bound.status if upper_bound else "unknown",
                    _decimal_to_text(upper_bound.amount) if upper_bound and upper_bound.amount is not None else None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()


def record_call_status(
    *,
    context: CostContext,
    registry_version: int,
    contract: Any,
    logical_call_id: str,
    attempt_id: str,
    request_sequence: int,
    retry_sequence: int,
    status: str,
    response: Any | None = None,
    error: BaseException | None = None,
) -> None:
    if status not in TERMINAL_STATUSES:
        raise ValueError("status must be a terminal ledger status.")
    logical_call_id = _bounded_text(logical_call_id, "logical_call_id")
    attempt_id = _bounded_text(attempt_id, "attempt_id")
    request_sequence = _positive_int(request_sequence, "request_sequence")
    retry_sequence = _nonnegative_int(retry_sequence, "retry_sequence")
    try:
        usage = extract_usage(response if error is None else error)
    except ValueError:
        usage = TokenUsage()
    pricing = _pricing_metadata(registry_version, contract)
    amount = _actual_or_partial_cost(pricing, usage, contract)
    now = _utc_now()
    error_type = _safe_error_type(error) if error is not None else None
    connection = _connect(context.db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            started = connection.execute(
                """
                SELECT started_at FROM remote_call_ledger
                WHERE attempt_id = ? AND status = 'started'
                ORDER BY id ASC LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
            started_at = started["started_at"] if started is not None else now
            connection.execute(
                """
                INSERT OR IGNORE INTO remote_call_ledger (
                    job_id, logical_call_id, attempt_id, stage, stage_attempt,
                    repair_attempt, recovery_attempt, provider_id, model_id,
                    request_sequence, retry_sequence, fallback_sequence,
                    pricing_contract_version, pricing_contract_fingerprint,
                    pricing_status, currency, cost_status, cost_amount,
                    input_tokens, output_tokens, cached_input_tokens,
                    reasoning_tokens, total_tokens, status, started_at,
                    completed_at, event_time, error_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.job_id,
                    logical_call_id,
                    attempt_id,
                    context.stage,
                    context.stage_attempt,
                    context.repair_attempt,
                    context.recovery_attempt,
                    _bounded_text(contract.provider_id, "provider_id"),
                    _bounded_text(contract.model_id, "model_id"),
                    request_sequence,
                    retry_sequence,
                    context.fallback_sequence,
                    pricing.pricing_contract_version,
                    pricing.pricing_contract_fingerprint,
                    pricing.pricing_status,
                    amount.currency,
                    amount.status,
                    _decimal_to_text(amount.amount)
                    if amount.amount is not None
                    else None,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_input_tokens,
                    usage.reasoning_tokens,
                    usage.total_tokens,
                    status,
                    started_at,
                    now,
                    now,
                    error_type,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()


def extract_usage(value: Any) -> TokenUsage:
    usage = _usage_mapping(value)
    if usage is None:
        return TokenUsage()
    input_tokens = _optional_token(
        usage.get("prompt_tokens", usage.get("input_tokens")),
        "input_tokens",
    )
    output_tokens = _optional_token(
        usage.get("completion_tokens", usage.get("output_tokens")),
        "output_tokens",
    )
    total_tokens = _optional_token(usage.get("total_tokens"), "total_tokens")
    cached_input_tokens = _optional_token(
        usage.get("cached_tokens", usage.get("cached_input_tokens")),
        "cached_input_tokens",
    )
    prompt_details = usage.get("prompt_tokens_details") or usage.get(
        "input_tokens_details"
    )
    if cached_input_tokens is None and isinstance(prompt_details, Mapping):
        cached_input_tokens = _optional_token(
            prompt_details.get("cached_tokens"),
            "cached_input_tokens",
        )
    reasoning_tokens = _optional_token(
        usage.get("reasoning_tokens"),
        "reasoning_tokens",
    )
    completion_details = usage.get("completion_tokens_details") or usage.get(
        "output_tokens_details"
    )
    if reasoning_tokens is None and isinstance(completion_details, Mapping):
        reasoning_tokens = _optional_token(
            completion_details.get("reasoning_tokens"),
            "reasoning_tokens",
        )
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        present=True,
    )


def summarize_job_costs(path: Path | str, job_id: str) -> dict[str, Any]:
    connection = _connect(Path(path))
    try:
        job = connection.execute(
            """
            SELECT cost_budget_policy, cost_budget_currency, cost_budget_amount
            FROM jobs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if job is None:
            raise CostLedgerError("Cost ledger job context is not available.")
        rows = connection.execute(
            """
            SELECT * FROM remote_call_ledger
            WHERE job_id = ?
            ORDER BY id ASC
            """,
            (job_id,),
        ).fetchall()
    finally:
        connection.close()

    latest = _latest_rows_by_attempt(rows)
    status_counts: dict[str, int] = {}
    actual_by_currency: dict[str, Decimal] = {}
    estimated_by_currency: dict[str, Decimal] = {}
    reserved_by_currency: dict[str, Decimal] = {}
    unknown_attempts = 0
    for row in latest:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
        cost_status = str(row["cost_status"])
        currency = row["currency"]
        amount = _optional_decimal(row["cost_amount"])
        if cost_status == "unknown" or currency is None or amount is None:
            unknown_attempts += 1
            continue
        bucket = (
            reserved_by_currency
            if status == "started"
            else actual_by_currency
            if cost_status == "actual"
            else estimated_by_currency
        )
        currency_key = str(currency)
        bucket[currency_key] = bucket.get(currency_key, Decimal("0")) + amount
    return {
        "attempt_count": len(latest),
        "status_counts": dict(sorted(status_counts.items())),
        "actual_by_currency": _format_currency_map(actual_by_currency),
        "estimated_by_currency": _format_currency_map(estimated_by_currency),
        "reserved_by_currency": _format_currency_map(reserved_by_currency),
        "unknown_attempts": unknown_attempts,
        "budget_policy": str(job["cost_budget_policy"] or "none"),
        "budget_currency": job["cost_budget_currency"],
        "budget_amount": job["cost_budget_amount"],
    }


def cancellation_status(error: BaseException) -> str:
    name = type(error).__name__.lower()
    if "cancel" in name or name == "keyboardinterrupt":
        return "cancelled"
    return "failed"


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _bounded_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{field_name} must be a non-empty bounded string.")
    if not value.isascii() or not value.isprintable():
        raise ValueError(f"{field_name} must be printable ASCII.")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    integer = _nonnegative_int(value, field_name)
    if integer < 1:
        raise ValueError(f"{field_name} must be positive.")
    return integer


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer.")
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a non-negative integer.") from exc
    if integer < 0 or str(value).strip() not in {str(integer), f"+{integer}"}:
        if not isinstance(value, int):
            raise ValueError(f"{field_name} must be a non-negative integer.")
    return integer


def _optional_token(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Decimal value is required.")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Decimal value is invalid.") from exc
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("Decimal value must be finite and non-negative.")
    return decimal


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value)


def _decimal_to_text(value: Decimal) -> str:
    if value == value.to_integral_value():
        return format(value.quantize(Decimal("1")), "f")
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _format_currency_map(values: Mapping[str, Decimal]) -> dict[str, str]:
    return {
        currency: _decimal_to_text(amount)
        for currency, amount in sorted(values.items())
    }


def _pricing_metadata(registry_version: int, contract: Any) -> CostAmount:
    pricing = contract.pricing
    pricing_status = str(getattr(pricing, "status", "unknown"))
    payload = {
        "registry_version": registry_version,
        "provider_id": contract.provider_id,
        "model_id": contract.model_id,
        "pricing": {
            "status": pricing_status,
            "currency": getattr(pricing, "currency", None),
            "input_per_million": getattr(pricing, "input_per_million", None),
            "cached_input_per_million": getattr(
                pricing, "cached_input_per_million", None
            ),
            "output_per_million": getattr(pricing, "output_per_million", None),
            "effective_date": getattr(pricing, "effective_date", None),
        },
        "max_output_tokens": getattr(contract, "max_output_tokens", None),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return CostAmount(
        status="unknown",
        currency=getattr(pricing, "currency", None),
        amount=None,
        pricing_status=pricing_status,
        pricing_contract_version=str(registry_version),
        pricing_contract_fingerprint=sha256(encoded).hexdigest(),
    )


def _prices(pricing: Any) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if getattr(pricing, "status", None) != "configured":
        return None, None, None
    input_price = _decimal(getattr(pricing, "input_per_million", None))
    cached_input_price = (
        None
        if getattr(pricing, "cached_input_per_million", None) is None
        else _decimal(getattr(pricing, "cached_input_per_million", None))
    )
    output_price = _decimal(getattr(pricing, "output_per_million", None))
    return input_price, cached_input_price, output_price


def _estimated_upper_bound(
    pricing_metadata: CostAmount,
    contract: Any,
    request: Mapping[str, Any],
) -> CostAmount | None:
    pricing = contract.pricing
    if getattr(pricing, "status", None) != "configured" or not pricing_metadata.currency:
        return None
    input_tokens = request.get("_input_token_count")
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        return None
    requested_output = request.get("max_completion_tokens", request.get("max_tokens"))
    if requested_output is None:
        requested_output = getattr(contract, "max_output_tokens", None)
    if (
        isinstance(requested_output, bool)
        or not isinstance(requested_output, int)
        or requested_output < 0
    ):
        return None
    input_price, _, output_price = _prices(pricing)
    if input_price is None or output_price is None:
        return None
    amount = (
        Decimal(input_tokens) * input_price
        + Decimal(requested_output) * output_price
    ) / MILLION
    return CostAmount(
        status="estimated",
        currency=pricing_metadata.currency,
        amount=amount,
        pricing_status=pricing_metadata.pricing_status,
        pricing_contract_version=pricing_metadata.pricing_contract_version,
        pricing_contract_fingerprint=pricing_metadata.pricing_contract_fingerprint,
    )


def _actual_or_partial_cost(
    pricing_metadata: CostAmount,
    usage: TokenUsage,
    contract: Any,
) -> CostAmount:
    pricing = contract.pricing
    if (
        getattr(pricing, "status", None) != "configured"
        or not pricing_metadata.currency
        or not usage.present
    ):
        return pricing_metadata
    input_price, cached_input_price, output_price = _prices(pricing)
    if input_price is None or output_price is None:
        return pricing_metadata

    known_components: list[Decimal] = []
    complete = True
    if usage.input_tokens is None:
        complete = False
    else:
        if usage.cached_input_tokens is None:
            complete = False if cached_input_price is not None else complete
            known_components.append(Decimal(usage.input_tokens) * input_price)
        elif usage.cached_input_tokens > usage.input_tokens:
            complete = False
            known_components.append(Decimal(usage.input_tokens) * input_price)
        else:
            regular_input = usage.input_tokens - usage.cached_input_tokens
            cached_price = cached_input_price if cached_input_price is not None else input_price
            known_components.append(Decimal(regular_input) * input_price)
            known_components.append(Decimal(usage.cached_input_tokens) * cached_price)

    if usage.output_tokens is None:
        complete = False
    else:
        known_components.append(Decimal(usage.output_tokens) * output_price)

    if not known_components:
        return pricing_metadata
    amount = sum(known_components, Decimal("0")) / MILLION
    return CostAmount(
        status="actual" if complete else "estimated",
        currency=pricing_metadata.currency,
        amount=amount,
        pricing_status=pricing_metadata.pricing_status,
        pricing_contract_version=pricing_metadata.pricing_contract_version,
        pricing_contract_fingerprint=pricing_metadata.pricing_contract_fingerprint,
    )


def _usage_mapping(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        candidate = value.get("usage")
        return candidate if isinstance(candidate, Mapping) else None
    direct_usage = getattr(value, "usage", None)
    if isinstance(direct_usage, Mapping):
        return direct_usage
    response = getattr(value, "response", None)
    response_usage = getattr(response, "usage", None)
    if isinstance(response_usage, Mapping):
        return response_usage
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception:
            dumped = None
        if isinstance(dumped, Mapping) and isinstance(dumped.get("usage"), Mapping):
            return dumped["usage"]
    model_dump_json = getattr(value, "model_dump_json", None)
    if callable(model_dump_json):
        try:
            dumped_json = json.loads(model_dump_json())
        except Exception:
            dumped_json = None
        if isinstance(dumped_json, Mapping) and isinstance(
            dumped_json.get("usage"),
            Mapping,
        ):
            return dumped_json["usage"]
    body = getattr(value, "body", None)
    if isinstance(body, Mapping) and isinstance(body.get("usage"), Mapping):
        return body["usage"]
    return None


def _attempt_row_exists(connection: sqlite3.Connection, attempt_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM remote_call_ledger WHERE attempt_id = ? LIMIT 1",
            (attempt_id,),
        ).fetchone()
        is not None
    )


def _assert_hard_budget_allows(
    connection: sqlite3.Connection,
    job_id: str,
    budget_currency: str,
    budget_amount_text: str,
    upper_bound: CostAmount | None,
) -> None:
    if upper_bound is None or upper_bound.amount is None or upper_bound.currency is None:
        raise CostBudgetUnknownError(
            "Hard budget requires configured pricing, known input tokens, and a bounded output token limit."
        )
    if not budget_currency or not budget_amount_text:
        raise CostBudgetUnknownError("Hard budget is missing a currency or amount.")
    budget_amount = _decimal(budget_amount_text)
    if upper_bound.currency != budget_currency:
        raise CostBudgetUnknownError(
            "Hard budget currency must match the remote call currency."
        )
    current = _budget_committed_amount(connection, job_id, budget_currency)
    if current is None:
        raise CostBudgetUnknownError(
            "Hard budget cannot continue while previous call costs are unknown."
        )
    if current + upper_bound.amount > budget_amount:
        raise CostBudgetExceededError("Hard budget would be exceeded by this call.")


def _budget_committed_amount(
    connection: sqlite3.Connection,
    job_id: str,
    currency: str,
) -> Decimal | None:
    rows = connection.execute(
        """
        SELECT * FROM remote_call_ledger
        WHERE job_id = ?
        ORDER BY id ASC
        """,
        (job_id,),
    ).fetchall()
    total = Decimal("0")
    for row in _latest_rows_by_attempt(rows):
        cost_status = str(row["cost_status"])
        amount = _optional_decimal(row["cost_amount"])
        row_currency = row["currency"]
        if row["status"] == "started":
            if cost_status != "estimated" or row_currency != currency or amount is None:
                return None
            total += amount
            continue
        if cost_status != "actual" or row_currency != currency or amount is None:
            return None
        total += amount
    return total


def _latest_rows_by_attempt(rows: list[sqlite3.Row] | tuple[sqlite3.Row, ...]) -> list[sqlite3.Row]:
    by_attempt: dict[str, sqlite3.Row] = {}
    terminal_priority = {"completed": 3, "failed": 2, "cancelled": 2, "started": 1}
    for row in rows:
        attempt_id = str(row["attempt_id"])
        existing = by_attempt.get(attempt_id)
        if existing is None:
            by_attempt[attempt_id] = row
            continue
        row_rank = terminal_priority.get(str(row["status"]), 0)
        existing_rank = terminal_priority.get(str(existing["status"]), 0)
        if row_rank > existing_rank or (
            row_rank == existing_rank and int(row["id"]) > int(existing["id"])
        ):
            by_attempt[attempt_id] = row
    return sorted(by_attempt.values(), key=lambda item: int(item["id"]))


def _safe_error_type(error: BaseException | None) -> str | None:
    if error is None:
        return None
    return _bounded_text(type(error).__name__, "error_type")


def new_logical_call_id() -> str:
    return uuid.uuid4().hex


def new_attempt_id() -> str:
    return uuid.uuid4().hex
