# ADR 0008: Append-only Cost Ledger and Budget Enforcement

Date: 2026-08-31

## Status

Accepted for PR-06B.

## Context

PR-04B introduced stage-boundary at-least-once recovery. A recovered stage may repeat a remote LLM call, so the control plane must treat repeated external attempts as real costs. PR-05 introduced explicit Provider/Model contracts and pricing metadata, but unverified prices remain `unknown`. PR-06A separated execution, evaluation, quality, and repair states without implementing cost governance.

Cost governance must not store secrets, prompts, complete provider responses, local credential paths, or guessed price data. It also must avoid binary floating-point accumulation for money and must not merge currencies through guessed exchange rates.

## Decision

Add an append-only SQLite table, `remote_call_ledger`, for remote LLM attempts that pass through `codes/provider_registry.py`. Each real attempt writes a `started` event before transport and a terminal `completed`, `failed`, or `cancelled` event after transport. Retries, fallbacks, split `max_n=1` requests, repair attempts, and PR-04B recovery reruns are separate attempts. Duplicate inserts for the same `(attempt_id, status)` are ignored by a unique constraint so repeated ledger writes are idempotent and do not double-count.

Ledger rows store only bounded, non-sensitive fields:

- job/stage/repair/recovery attempt context
- `logical_call_id` and `attempt_id`
- provider/model IDs
- request, retry, and fallback sequence numbers
- available usage token counts
- pricing contract version/fingerprint
- currency
- `actual`, `estimated`, or `unknown` cost status
- Decimal amount encoded as text
- started/completed/failed/cancelled timestamps

Ledger rows do not store API keys, Authorization headers, prompts, full provider responses, base URLs, local credential paths, complete commands, or environment snapshots.

Hard budgets are optional per SQLite job. The default is no budget. A hard budget requires a currency and Decimal string amount. Before a remote call, the provider wrapper opens a SQLite `BEGIN IMMEDIATE` transaction, computes a conservative upper bound from verified pricing, known input tokens, and a bounded output token limit, then checks current committed or in-flight reserved costs in the same currency.

If the upper bound cannot be determined, hard budget fails closed and does not call the provider. Unknown pricing, missing input tokens, missing output bounds, currency mismatch, or previous unknown terminal costs all block further hard-budget calls.

Cost summaries are job-level only and group amounts by currency. They expose status counts, unknown attempt counts, and `actual`, `estimated`, and reserved amounts by currency. They do not expose attempt IDs or internal ledger details.

## Consequences

This preserves actual provider cost semantics under at-least-once recovery and hidden SDK retries. When the ledger is enabled, SDK automatic retries are disabled and provider registry performs explicit retry attempts so every retry is visible in the ledger.

Missing usage remains unknown, not zero. Unverified pricing remains unknown, not guessed. Partial usage can produce an estimated known component, while unknown components remain visible through status. Multi-currency jobs are possible without exchange-rate conversion.

Hard budget may reject calls that could have succeeded cheaply if a safe upper bound is unavailable. This is intentional: spending control is safer when uncertainty blocks the call before transport.

## Non-goals

PR-06B does not implement SSE, a WebUI cost panel, exchange-rate services, provider price discovery, or real paid-provider tests.
