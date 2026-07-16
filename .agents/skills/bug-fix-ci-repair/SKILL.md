---
name: bug-fix-ci-repair
description: Diagnose and repair failing tests, build errors, runtime bugs, regressions, and CI-style failures. Use when the user provides an error log, says tests or checks fail, asks to fix a bug, or wants a failure root cause plus a verified patch.
---

# Bug Fix CI Repair

## Overview

Find the smallest real cause of a failure, fix it without masking symptoms, and verify the affected path.

## Workflow

1. Capture the failure.
   - Read the exact error, stack trace, failing assertion, exit code, and relevant command.
   - Reproduce locally when feasible using the narrowest command.
   - If reproduction is expensive, inspect the failing code path and available logs first.

2. Isolate root cause.
   - Trace from the failing output to the responsible module.
   - Check recent or related code only as needed.
   - Separate product bug, test bug, environment issue, missing dependency, flaky timing, and changed external contract.

3. Patch narrowly.
   - Fix behavior rather than weakening tests.
   - Add or update regression coverage when the bug is behaviorally meaningful.
   - Preserve public interfaces unless the failure proves the interface is wrong.

4. Verify the repair.
   - Run the original failing command.
   - Run adjacent tests or checks when the fix touches shared code.
   - Record any skipped verification and why.

## Root Cause Report

When finishing, include:

- Symptom
- Root cause
- Fix summary
- Verification commands and results
- Remaining risk, if any
