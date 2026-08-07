# Evaluation Contract and Repair Policy

Date: 2026-08-07

## Summary

PR-06A introduces an explicit, versioned evaluation result contract and repair
policy. Evaluation execution, evaluator protocol state, quality verdict, score,
and repair eligibility are now represented separately. Evaluator protocol
failures no longer masquerade as low-quality code, and repair is limited to
validated TaskManifest files.

## Background

The legacy flow relied on one repository status string to drive both quality
interpretation and auto-repair. Malformed evaluator output, provider timeout,
provider protocol errors, evaluator unavailability, quorum failure, and real
quality rejection could all reach similar auto-refine paths. The repair stage
also treated an empty repair file list as a broad manifest repair request.

## Changes

- Added `codes/evaluation_contract.py` for schema v1 validation, quorum
  decisions, sanitized evaluator error results, repair decisions, and
  `files_to_fix` path binding.
- Updated `codes/eval.py` to persist schema-based evaluation results without
  prompts, request JSON, full provider responses, credentials, or raw model
  payloads.
- Updated Pipeline and standalone auto-refine loops so repair only runs for
  schema-approved quality rejections.
- Updated `codes/3_coding.py` so `files_to_fix=[]` means no file changes, and
  every non-empty repair target is revalidated against the manifest and target
  repository path before writes.
- Updated SQLite state handling so evaluator completion is
  `evaluation_status=completed`, while the code-quality outcome lives in
  `quality_status`.
- Added SQLite migration 7 for legacy status compatibility and the structured
  `repair_attempts` table.
- Added ADR 0007 and focused regression tests.
- Hardened local settings ACL principal detection for the sandboxed Windows
  test environment, preserving the existing structured settings error
  contract.

## Runtime Behavior

- Execution failure produces `evaluation_status=skipped`,
  `quality_status=skipped`, and repair is blocked.
- Timeout, provider protocol error, evaluator unavailable, malformed evaluator
  output, and quorum failure produce `evaluation_status=failed`,
  `quality_verdict=not_assessed`, and repair is blocked.
- Accepted quality produces no repair.
- Rejected quality plus a non-empty, validated `files_to_fix` list and remaining
  attempt budget produces a repair-ready decision.
- `files_to_fix=[]` produces `repair_status=skipped` and modifies no files.

## Scope Notes

Provider/model fallback remains governed by the PR-05 Registry. Cost ledger,
budget enforcement, SSE, command APIs, live WebUI controls, Playwright/CI
rework, and real-model quality scoring remain outside PR-06A.

## Verification

- Failure-first:
  `.\.venv\Scripts\python.exe -m pytest tests\test_evaluation_contract.py -q -p no:cacheprovider --basetemp .pytest_tmp_pr06a_failure`
  -> `18 failed, 1 passed` before implementation.
- Targeted PR-06A and regression checks:
  `.\.venv\Scripts\python.exe -m pytest tests\test_evaluation_contract.py tests\test_eval_fallback_and_params.py tests\test_task_manifest.py tests\test_job_repository.py tests\test_sqlite_runtime.py tests\test_api_contract.py::test_settings_not_configured_create_job_returns_structured_error tests\test_settings_store_resilience.py -q -p no:cacheprovider --basetemp .pytest_tmp_pr06a_final_targeted2`
  -> `230 passed, 1 warning`.
- Compile:
  `.\.venv\Scripts\python.exe -m compileall -q codes web_api tests`
  -> passed.
- Full pytest in sandbox:
  `.\.venv\Scripts\python.exe -m pytest tests -q --tb=short -rs -p no:cacheprovider --basetemp .pytest_tmp_pr06a_full_sandbox_all2`
  -> `585 passed, 2 failed, 1 warning`; both failures were Windows
  `taskkill` access-denied cleanup tests.
- Windows process cleanup tests with external permission:
  `.\.venv\Scripts\python.exe -m pytest tests\test_process_lifecycle.py::test_cancel_job_terminates_child_process_tree tests\test_worker_runtime.py::test_startup_reconciliation_marks_dead_process_as_explainable_failure -q -p no:cacheprovider --basetemp .pytest_tmp_pr06a_windows_process_escalated2`
  -> `2 passed, 1 warning`.
- WebUI:
  `npm run typecheck`, `npm run build`, `npm run verify:same-origin`,
  `npm run smoke`, and external-permission `npm run smoke:prod` all passed.
