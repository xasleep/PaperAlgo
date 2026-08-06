# ADR 0006: Explicit Provider/Model Registry

## Status

Accepted for PR-05.

## Context

The previous OpenAI-compatible adapter inferred a provider from model-name prefixes and, for unknown names, selected whichever API key/base URL happened to exist. Evaluation separately inferred per-request `n` limits and used a hard-coded 128k context gate. This could bind a valid model name to the wrong endpoint, allow invalid settings into the queue, and report costs from undated tables.

## Decision

Remote LLM selection is the ordered pair `(provider_id, model_id)`. The versioned `codes/providers.v1.json` file declares every accepted pair and its base URL policy, API-key environment variable name, `max_n`, context/output limits, JSON Schema/usage/cache-token support, timeout, retry, concurrency, and dated pricing state. Unknown facts are represented as `null` or `unknown`; they are not inferred.

`codes/provider_registry.py` validates the file, resolves credentials without retaining them in representations or errors, builds a client with an explicit base URL, and fences every chat-completion request against the selected contract. DeepSeek, Qwen, Kimi, Claude, and OpenAI use distinct credential/base-URL names. No provider silently falls back to OpenAI.

Registry v1 is a closed schema at the root, provider, model, pricing, and request-options levels. Unknown fields, booleans used as numbers, non-finite values, unsafe ranges, malformed response formats, duplicate/self/cross-provider fallbacks, malformed JSON, and unreadable files fail as sanitized `invalid_provider_registry`. Loaded contracts and nested request options are deeply immutable. Transport timeout, 429, and 5xx errors are not converted into success.

Pipeline subprocess environments are projections, not copies of the parent environment. For each role, `REPRODUCE_*` or `EVAL_*` overrides the selected model contract's provider-native environment names; otherwise only those provider-native names are read and re-injected. A Registry `base_url` is the final non-secret fallback. Credentials for unselected providers and for the other role are never propagated.

Web settings are validated before persistence and again before a new job enters either runtime queue. Unknown provider/model, a blank key, or a required missing base URL produces a sanitized HTTP 422. Pipeline and stage CLIs keep their existing provider/model migration surface, but every remote Planning, Analysis, Coding, Repair, Evaluation, RAG-config, and debugging request obtains its client through the Registry.

`GET /api/v1/providers` is the single non-secret discovery surface for the WebUI. It is generated from the loaded Registry, omits providers without active models, returns only IDs and capability state, and is marked `no-store`. The settings status endpoint remains boolean-only; the UI neither reconstructs a provider allowlist nor reads back saved credentials.

For SQLite runtime, job creation persists a non-sensitive provider-selection snapshot: both primary provider/model pairs, evaluation fallbacks, Registry version, and a deterministic SHA-256 over the selected contracts. API keys and base URLs are excluded. The Worker accepts rotated credentials only when the selection and contract fingerprint still match; otherwise it fails before `Popen` with `provider_settings_changed`. Missing snapshots on legacy queued/running rows also fail closed. Idempotency replay keeps the original job and snapshot. Legacy runtime launch semantics are unchanged.

Evaluation sends explicit `n` only when a request needs more than one candidate. Models with `max_n=1` fan out `generated_n` as independent synchronous single-candidate requests, bounded by the existing 1–32 candidate budget and the Registry concurrency semaphore. Fallback model IDs are parsed literally, resolved under the same explicit provider, and rejected before client creation when duplicate, empty, blank, malformed, self-referential, cross-provider, or unknown; a model name cannot silently switch providers.

Only pricing marked `configured` with currency, input/output rates, and an effective date is used. Otherwise cost is unavailable. Partial usage remains partial and is not filled with invented cache-token data.

The bundled allowlist is PaperAlgo's current supported catalog, not a mirror of every official model. It was verified on 2026-08-06 against first-party documentation:

- DeepSeek: [`deepseek-v4-flash` and `deepseek-v4-pro`](https://api-docs.deepseek.com/quick_start/pricing/). The legacy `deepseek-chat` and `deepseek-reasoner` aliases are excluded from new-task selection because DeepSeek states they become inaccessible after 2026-07-24 15:59 UTC.
- Alibaba Cloud Model Studio: [`qwen3.8-max`, `qwen3.7-max`, and `qwen3.7-plus`](https://help.aliyun.com/en/model-studio/models). Hyphenated `qwen-3.*` spellings are not official model IDs and are excluded.
- Kimi API Platform: [`kimi-k3`, `kimi-k2.7-code`, `kimi-k2.7-code-highspeed`, and `kimi-k2.6`](https://platform.kimi.ai/docs/models). `kimi-k3` uses empty `request_options`; PaperAlgo omits fixed parameters that the [Kimi K3 limits](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart#important-limits) say should not be sent. Earlier Kimi/Moonshot generations that are unavailable to new users or scheduled for sunset are excluded from new-task selection.
- OpenAI: active support is limited to [`gpt-4.1-mini`](https://developers.openai.com/api/docs/models/gpt-4.1-mini) and [`gpt-4o-mini`](https://developers.openai.com/api/docs/models/gpt-4o-mini). `o3-mini` and `o4-mini` are excluded because the [OpenAI deprecations page](https://developers.openai.com/api/docs/deprecations) lists them as Deprecated.

Official pages may describe JSON output or structured output, but these terms are not automatically treated as JSON Schema support. Context/output fields and pricing remain `null`/`unknown` unless the Registry review records values with unambiguous units and, for pricing, the complete currency/rate/effective-date tuple.

## Consequences

- Adding or changing a remote model is an explicit configuration review, not a model-prefix code change.
- Bundled `base_url: null` is a deliberate require-explicit-endpoint policy for local configuration and regional/OpenAI-compatible endpoint choice; it does not mean the vendor's official endpoint is unknown.
- Existing cross-provider fallback lists without provider IDs are rejected instead of guessed.
- Legacy local vLLM scripts remain a separate offline execution path; this ADR governs remote OpenAI-compatible requests and does not introduce a new orchestration framework.
- Registry enforcement does not store API keys, prompts, full model responses, commands, or environments in SQLite, events, or status APIs.
- API keys remain plaintext in the local ignored settings file and transient child environment; this is not encrypted credential storage. The deployment boundary remains Windows, loopback-only, local single-user, one FastAPI process, and one Worker.
- PR-06 cost-ledger work and PR-07 SSE remain outside this decision.
