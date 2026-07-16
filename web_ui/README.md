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

In development, the frontend uses `http://localhost:8000` by default. The FastAPI
backend allows cross-origin requests only from the local Vite dev origins
`http://localhost:5173` and `http://127.0.0.1:5173`, so Save Settings and other
API calls work directly in the default Vite dev mode without opening the backend
to public origins.

Override the API base URL when needed:

```powershell
$env:VITE_API_BASE_URL="http://localhost:8000"
npm run dev
```

The Vite `/api` proxy remains available as a fallback for unusual local setups:

```powershell
$env:VITE_API_BASE_URL="/api"
npm run dev
```

## Production Build

Build the frontend for FastAPI same-origin serving:

```powershell
Set-Location .\web_ui
npm run build
npm run verify:same-origin
```

Production builds use same-origin API paths by default. If you need a custom backend address in a production build, set `VITE_API_BASE_URL` explicitly:

```powershell
$env:VITE_API_BASE_URL="https://example.internal"
npm run build
```

Then start FastAPI from the repository root:

```powershell
Set-Location ..
.\.venv\Scripts\uvicorn.exe web_api.main:app --host 127.0.0.1 --port 8000
```

When `web_ui/dist/index.html` exists, FastAPI serves the built frontend at `/` and serves static assets from `/assets`. API routes such as `/health`, `/settings/status`, and `/jobs` keep their existing JSON behavior for API clients.

Browser navigations that request HTML can receive the frontend shell for client-side routes. API calls should send `Accept: application/json`, which the WebUI client already does.

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

The production smoke check starts a temporary FastAPI server on a free local port, verifies built `web_ui/dist` HTML routes, and confirms `/health` and `/jobs` still return JSON for API clients. Run `npm run build` before `npm run smoke:prod`.
