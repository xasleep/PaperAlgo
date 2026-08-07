# ADR 0007: Evaluation Contract and Repair Policy

## Status

Accepted for PR-06A.

## Context

The legacy evaluation loop used one repository status string to represent both evaluator execution and code quality. A malformed evaluator response, timeout, provider protocol error, or unavailable evaluator could therefore look similar to "the generated code is low quality". The repair loop also treated an empty repair file list as a broad repair request, which could modify every manifest file.

## Decision

Evaluation results now use `codes/evaluation_contract.py` as the versioned contract boundary. Schema v1 separates:

- `execution_status`: whether the Pipeline execution reached evaluation.
- `evaluation_status`: whether the evaluator completed, failed, or was skipped.
- `quality_status` and `quality_verdict`: whether code quality was accepted, rejected, or not assessed.
- `repair_status`: whether repair is pending, blocked, skipped, or not applicable.

Quality verdicts require quorum. For `generated_n`, the required quorum is `floor(generated_n / 2) + 1`; insufficient valid evaluator responses produce `evaluation_status=failed` and `quality_verdict=not_assessed`, not a quality rejection.

Evaluator timeout, provider protocol error, malformed evaluator output, unavailable evaluator, execution failure, and quorum failure are structured error codes. They block repair and store only sanitized messages. Evaluation result, feedback, status, SQLite events, and summaries must not store prompts, full provider responses, credentials, Authorization headers, or raw model payloads.

Repair is only allowed when the schema says quality was rejected and `decide_repair_action()` returns `ready`. `files_to_fix=[]` means "modify no files". It never expands to the whole repository or whole manifest. Non-empty repair paths are revalidated against the persisted TaskManifest and the target repo path before any file write. Absolute paths, traversal, symlink/reparse escapes, and manifest-external files are rejected.

Repair attempts are capped by `max_repair_rounds`. Completed repair attempts are represented structurally so retry/recovery can avoid repeating a repair that was already confirmed complete.

SQLite job state now treats `evaluation_status=completed` as the successful evaluator completion state. Code quality moves through `quality_status`. Migration 7 maps legacy completed jobs with `evaluation_status=passed` to `evaluation_status=completed, quality_status=accepted`, and legacy quality failures from `evaluation_status=failed, quality_status=pending` to `evaluation_status=completed, quality_status=rejected`. It also adds `repair_attempts` for structured repair tracking. PR-06A does not implement cost ledger or budget enforcement.

## Consequences

- Provider Registry fallback remains PR-05 scoped: fallback IDs must be registered for the same explicit provider/model contract before any client is created.
- Auto-refine and Pipeline repair no longer run for evaluator protocol failures, quorum failures, execution failures, or empty repair targets.
- Existing legacy status strings are preserved for compatibility, but new fields carry the authoritative contract.
- SSE, command APIs, live WebUI control, cost ledger, and real-model quality evaluation remain outside PR-06A.
