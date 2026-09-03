# ADR 0011: Release Engineering and Clean-checkout Hardening

- Status: Accepted
- Date: 2026-09-03

## Context

The project now has a Windows local WebUI, FastAPI control plane, SQLite Worker,
provider registry, append-only cost ledger, replayable SSE, and live job controls.
Release validation needed to prove that a fresh checkout can be installed and
tested without relying on a developer machine's existing state. The previous CI
installed a small hard-coded Python dependency list, had no cache key tied to the
new dependency constraints, and did not exercise the WebUI job flow through a
browser with fake providers.

The repo also needs to keep runtime state, databases, logs, run outputs, coverage
and credentials out of Git history. Optional local-model dependencies such as
vLLM and other heavy provider integrations have different platform and install
costs from the normal Windows Web/API runtime.

## Decision

- Python dependencies are split into `requirements-runtime.txt`,
  `requirements-dev.txt`, and `requirements-optional-heavy.txt`, all constrained
  by `constraints.txt`.
- `requirements.txt` remains a backward-compatible aggregate entrypoint for
  runtime plus optional-heavy dependencies.
- The default Windows install path uses `requirements-dev.txt`; optional heavy
  dependencies are installed only when explicitly needed in a compatible separate
  environment.
- Frontend dependencies remain locked by `web_ui/package-lock.json`.
- Local state can be redirected with `PAPER2CODE_LOCAL_DIR`,
  `PAPER2CODE_RUNS_DIR`, and `PAPER2CODE_DB_PATH`, allowing clean-checkout tests
  to use temporary SQLite databases and temporary runs without touching existing
  local state.
- GitHub Actions remains least privilege with `permissions: contents: read`,
  does not use `pull_request_target`, and runs the formal CI gate on Windows.
- CI installs Python dependencies from `requirements-dev.txt` and caches Python
  downloads using a key tied to `requirements-runtime.txt`,
  `requirements-dev.txt`, and `constraints.txt`.
- CI installs WebUI dependencies with `npm ci` and caches npm downloads using a
  key tied to `web_ui/package-lock.json`.
- CI runs backend pytest, Python compile checks, WebUI typecheck, build,
  same-origin verification, development smoke, production smoke, and fake-provider
  Playwright E2E.
- The fake-provider E2E uses a temporary provider registry, temporary settings,
  temporary SQLite database, temporary runs directory, local loopback FastAPI,
  and a browser driven by `playwright-core`. It does not call real paid providers,
  MinerU, vLLM, or the full long-running pipeline.
- CI avoids uploading artifacts, logs, settings files, prompts, API keys, or local
  paths as build artifacts.

## Consequences

Windows remains the formal supported platform. Any future Linux check must be
described as lightweight static/unit coverage unless the full install and E2E
contract is separately established for Linux.

Adding a new runtime dependency requires updating the appropriate requirements
layer and regenerating or reviewing `constraints.txt`. Adding a new frontend
dependency requires updating `web_ui/package-lock.json`. Dependency changes should
be compatibility- or security-driven, not version churn.

SQLite schema migration remains automatic and idempotent on API/Worker startup.
Users must stop local processes and back up `.local/paper2code.db*`,
`.local/web_settings.json`, and `runs/` before upgrading if rollback matters.

This decision does not publish a tag, GitHub Release, public deployment,
multi-user authentication, or real-provider E2E.
