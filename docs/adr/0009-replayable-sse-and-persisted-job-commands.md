# ADR 0009: Replayable SSE and Persisted Job Commands

- Status: Accepted
- Date: 2026-09-02

## Context

ADR 0003 selected SSE over WebSocket for one-way local job notifications. PR-04 through PR-06 added the SQLite single-worker runtime, process fencing, checkpoint recovery, provider contracts, evaluation/quality separation, and append-only cost ledger. PR-07A needs a backend event stream and durable job command control without changing the WebUI polling workflow yet.

## Decision

- Expose `GET /api/v1/jobs/{job_id}/events` for SQLite job events. The endpoint returns `text/event-stream`, `Cache-Control: no-store`, `X-Accel-Buffering: no`, and `Connection: keep-alive`.
- Persist event IDs in SQLite as monotonically increasing `job_events.id` values. `Last-Event-ID` resumes from the first newer event, and API/Worker restarts replay from SQLite rather than in-memory buffers.
- Keep replay bounded with a `replay_limit` parameter capped by the server. If more events are available than the bound, return a `stream.gap` SSE with `resync_required=true`; clients must read REST job status before reconnecting from a new cursor.
- Send heartbeat comments only as liveness hints. Heartbeats have no persisted event ID and do not replace REST status.
- Treat REST job status as authoritative. SSE payloads are incremental, sanitized, and size-limited; they do not contain API keys, prompts, full model responses, command summaries, process identities, launch tokens, instance tokens, or local filesystem paths.
- Persist job commands in `job_commands` with independent IDs, idempotency-key hashes, request status, rejection codes, result codes, and timestamps. Raw idempotency keys are never returned.
- Accept `approve`, `cancel`, `retry`, and `repair` only when the current SQLite job state allows the command. Invalid states and `identity_unresolved` become stable rejected command records queryable through the API.
- Worker command application remains lease-fenced by `worker_id + instance_token` and preserves launch-token and process-identity checks for process-affecting operations. Cancel keeps precedence over retry launches.
- Local same-origin, Origin/Referer, `Sec-Fetch-Site`, and CSRF boundaries remain in force. SSE performs explicit origin validation even though it is a GET endpoint.

## Consequences

The WebUI can continue polling while PR-07B later wires `EventSource` to these backend events. The implementation remains local, single FastAPI instance, single SQLite Worker, and non-distributed. It does not add WebSocket, multi-user auth, public internet deployment, or exactly-once pipeline execution.
