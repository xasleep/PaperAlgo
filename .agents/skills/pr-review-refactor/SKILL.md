---
name: pr-review-refactor
description: Review a change set and make focused refactors that improve correctness, maintainability, and testability. Use when the user asks for code review, PR readiness, cleanup after AI-generated code, risk assessment, refactoring, or a final quality pass before committing.
---

# PR Review Refactor

## Overview

Take a review-first posture: find bugs and risks before polishing style, then make narrow improvements that preserve intended behavior.

## Workflow

1. Establish the diff and intent.
   - Inspect changed files and nearby code.
   - Infer the requested behavior from the user prompt, tests, names, and surrounding patterns.
   - Ignore unrelated dirty worktree changes unless they affect the review.

2. Review for risk.
   - Prioritize correctness, regressions, data loss, security/privacy, race conditions, accessibility, performance, and missing tests.
   - Check API contracts, schema changes, error handling, edge cases, and compatibility.
   - Avoid broad rewrites unless the current implementation is genuinely unsafe or unmaintainable.

3. Refactor surgically.
   - Reduce duplication only when it simplifies real code paths.
   - Prefer existing local abstractions and naming.
   - Keep behavior stable unless fixing a discovered defect.
   - Add comments only where they clarify non-obvious logic.

4. Verify.
   - Run targeted tests, type checks, linters, or builds relevant to changed code.
   - If no tests exist, consider adding focused coverage for the riskiest behavior.
   - Report commands run and any gaps.

## Review Output

If the user asked only for review, lead with findings ordered by severity and include file/line references when available.

If the user asked to improve the code, finish with:

- What changed
- Why it changed
- Verification
- Residual risks or test gaps
