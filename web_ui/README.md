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
`/api/v1/health`, `/api/v1/settings/status`, and `/api/v1/jobs`.

The browser client obtains a local session from `/api/v1/session`, keeps the
returned CSRF token in memory, and sends it with state-changing requests. The
session cookie uses `SameSite=Strict`.

## Current Runtime Semantics

The job detail UI polls active jobs every 2 seconds. SSE and WebSocket event streams
are not implemented. In SQLite runtime, cancellation is asynchronous: the API creates one
idempotent cancel command and the independent Worker performs verified process-
tree termination. The response acknowledges the request; it does not mean the
job is already canceled. Legacy cancellation remains owned by the FastAPI
process that launched the Pipeline.

Settings status returns only boolean configuration flags and does not return API key
values. Keys are stored locally in `.local/web_settings.json` as plaintext JSON;
this local single-user boundary is not encrypted credential storage.

## Checks

```powershell
npm run typecheck
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
