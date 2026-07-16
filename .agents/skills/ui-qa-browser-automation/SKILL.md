---
name: ui-qa-browser-automation
description: Verify frontend UI behavior with browser-driven inspection and visual QA. Use when building or changing a web UI, checking responsive layout, testing flows, finding overlap/blank-screen issues, validating forms and navigation, or using Playwright/browser tools to inspect a running app.
---

# UI QA Browser Automation

## Overview

Use the app like a user, inspect what actually renders, and fix visible or interactive issues instead of relying on code inspection alone.

## Workflow

1. Start the app or locate the running URL.
   - Use the repository's existing dev command.
   - If a port is occupied, choose another available port when the app supports it.
   - Capture the URL for verification.

2. Inspect primary screens.
   - Check desktop and mobile-sized viewports.
   - Verify the first screen is the actual app experience, not filler.
   - Look for blank canvas, broken assets, clipped text, overflow, overlap, layout shift, inaccessible controls, and poor empty/loading states.

3. Exercise workflows.
   - Click primary navigation and controls.
   - Fill forms and trigger validation.
   - Check hover, disabled, loading, success, and error states when practical.
   - Use screenshots or DOM snapshots to ground findings.

4. Fix and re-check.
   - Patch CSS, markup, state, routing, or asset references as needed.
   - Re-run the browser check after each meaningful visual fix.
   - Prefer stable responsive constraints over viewport-scaled font hacks.

## Quality Bar

- Text fits containers at common mobile and desktop widths.
- Interactive elements have clear states and do not shift layout unexpectedly.
- Important media, icons, and canvases render nonblank.
- Forms and navigation can be completed with predictable feedback.
- Final response includes the tested URL, viewports or flows checked, and remaining gaps.
