# ADR 0010: WebUI Live Status and Persisted Job Controls

- Status: Accepted
- Date: 2026-09-03

## Context

PR-07A added replayable SQLite-backed job events and persisted job commands, but
the React task detail page still used periodic REST polling and only exposed the
legacy cancel control. Browser `EventSource` also cannot set arbitrary request
headers, so a page refresh cannot manually send a stored `Last-Event-ID` header.

## Decision

- The job detail page loads REST job and command snapshots before opening SSE.
- The page consumes same-origin `EventSource` URLs under `/api/v1` and keeps the
  production same-origin path plus the strict Vite `5173` development boundary.
- SSE event IDs are deduplicated in the reducer. Replayed or reconnected events
  at or below the current cursor do not mutate UI state twice.
- Replay gaps trigger a REST resync and then reconnect from the latest safe
  event ID. The backend keeps the existing `Last-Event-ID` header behavior and
  adds browser-compatible `last_event_id` query support.
- For native EventSource clients, `eventsource=1` returns a gap as HTTP 200 with
  a `stream.gap` event body so the browser can observe the resync instruction.
  Existing non-EventSource clients still receive the prior 409 gap response.
- Connection errors use bounded reconnect attempts. After the bound, the page
  switches to a visible 15 second REST fallback rather than running both SSE and
  2 second polling forever.
- Approve, cancel, retry, and repair buttons derive availability from execution,
  evaluation, quality, and process identity state. Command responses and command
  history drive pending, applied, and rejected display.
- Cost display lists actual, estimated, and reserved amounts per currency and
  keeps unknown attempts explicit. It does not show unknown cost as zero or merge
  currencies with a guessed exchange rate.
- UI error display strips credentials, prompts, full responses, base URLs, and
  local filesystem paths before rendering.

## Consequences

This PR keeps the local single-user model and does not add WebSocket, multi-user
auth, public deployment, formal Playwright E2E, CI dependency restructuring, or
real provider calls. REST job status remains the authoritative state source; SSE
is the live notification and incremental update channel.
