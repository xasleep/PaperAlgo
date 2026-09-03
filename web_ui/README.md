# Paper2Code Web UI

Minimal React + Vite console for the local Paper2Code FastAPI backend.

## Development

Start the FastAPI backend from the repository root:

```powershell
.\.venv\Scripts\uvicorn.exe web_api.main:app --reload --host 127.0.0.1 --port 8000
```

Start the Vite frontend:

```powershell
Set-Location .\web_ui
npm ci
npm run dev
```

Open `http://localhost:5173`.

The Vite dev server is pinned to port `5173`. If that port is already in use,
`npm run dev` fails instead of switching to `5174` or another port. Stop the
process that is using `5173`, then run `npm run dev` again. Do not change the
dev port unless you also update the FastAPI local CORS allowlist.

The recommended development setup keeps the browser on the same-origin Vite
proxy. The frontend uses the same-origin `/api/v1` path, and Vite proxies
that prefix to `http://localhost:8000` without rewriting it. FastAPI also keeps
an exact CORS allowlist for `http://localhost:5173` and
`http://127.0.0.1:5173`; no wildcard origin is enabled.

Override the API base URL when needed:

```powershell
$env:VITE_API_BASE_URL="http://127.0.0.1:8000"
npm run dev
```

`VITE_API_BASE_URL` is only the origin/base portion. The client always appends
the fixed `/api/v1` prefix. When using the `127.0.0.1` value above, open Vite at
`http://127.0.0.1:5173`; mixing a `localhost` frontend with a `127.0.0.1`
backend prevents the `SameSite=Strict` session cookie from being sent. Likewise,
when the backend uses `localhost`, open the frontend with `localhost` as well.

## Production Build

Build the frontend for FastAPI same-origin serving:

```powershell
Set-Location .\web_ui
npm run build
npm run verify:same-origin
```

Production builds use same-origin API paths by default. If you need a custom backend address in a production build, set `VITE_API_BASE_URL` explicitly:

```powershell
$env:VITE_API_BASE_URL="http://127.0.0.1:8000"
npm run build
```

Then start FastAPI from the repository root:

```powershell
Set-Location ..
.\.venv\Scripts\uvicorn.exe web_api.main:app --host 127.0.0.1 --port 8000
```

When `web_ui/dist/index.html` exists, FastAPI serves the built frontend at `/`
and static assets from `/assets`. `/jobs` is always a React route, independent
of `Accept`. JSON APIs live only below `/api/v1`, including
`/api/v1/health`, `/api/v1/providers`, `/api/v1/settings/status`, and `/api/v1/jobs`.

The browser client obtains a local session from `/api/v1/session`, keeps the
returned CSRF token in memory, and sends it with state-changing requests. The
session cookie uses `SameSite=Strict`.

## Current Runtime Semantics

The job detail UI loads an authoritative REST snapshot first, then follows the
SQLite runtime job stream with same-origin `EventSource`. Replay gaps trigger a
REST resync before the stream reconnects from the latest safe cursor. If SSE is
unavailable after bounded reconnect attempts, the page switches to an explicit
15 second REST fallback. It does not run the old infinite 2 second polling loop
at the same time as SSE.

In SQLite runtime, approve, cancel, retry, and repair are persisted asynchronous
job commands with an `Idempotency-Key`. The UI enables each button only in legal
states and shows pending, applied, or rejected from the persisted command record. Legacy
cancellation remains owned by the FastAPI process that launched the Pipeline;
if the command API is unavailable, the cancel button falls back to the legacy
cancel endpoint while approve, retry, and repair stay disabled.

Settings status returns only boolean configuration flags and does not return API key
values. Keys are stored locally in `.local/web_settings.json` as plaintext JSON;
this local single-user boundary is not encrypted credential storage.

The Settings page loads selectable Provider/Model pairs from the current
Registry through `GET /api/v1/providers`; it has no hard-coded vendor or model
allowlist. Providers without active models are omitted. Discovery returns only
non-sensitive IDs/capability state with `Cache-Control: no-store`. If discovery
fails, the form shows the structured API error and disables submission rather
than sending an unknown pair. Saved keys are never read back or displayed, and
base URLs remain explicit user input.

In SQLite runtime, a queued job keeps a non-sensitive selection snapshot. A
later settings change cannot silently rebind it: the Worker accepts rotated
credentials for the same selection, but fails before process creation when the
selection, Registry version, or contract fingerprint differs. Legacy runtime
behavior is unchanged. Cost display preserves unknown attempts and lists amounts
by currency without exchange-rate conversion. The current product remains
Windows/local single-user, loopback-only, one FastAPI process and one Worker;
there is no WebSocket, multi-user auth, distributed queue, formal Playwright E2E
framework, or real-provider smoke call.

## Checks

```powershell
npm run typecheck
npm run test:live-status
npm run build
npm run verify:same-origin
npm run smoke
npm run smoke:prod
cd ..
.\.venv\Scripts\python.exe -m pytest tests -q
```

The smoke check starts a temporary Vite server on `127.0.0.1:5199` and verifies the WebUI routes return the React shell without requiring API keys or a running pipeline.

The production smoke check starts a temporary FastAPI server on a free local
port, verifies built `web_ui/dist` HTML routes, and confirms `/api/v1/health`
and `/api/v1/jobs` return JSON. Run `npm run build` before
`npm run smoke:prod`.
