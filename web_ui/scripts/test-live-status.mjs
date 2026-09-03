import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import ts from "typescript";

async function importTypescriptModule(path) {
  const source = await readFile(path, "utf-8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2020,
    },
  }).outputText;
  const dir = await mkdtemp(join(tmpdir(), "paper2code-live-status-"));
  const modulePath = join(dir, "module.mjs");
  await writeFile(modulePath, output, "utf-8");
  try {
    return await import(`file:///${modulePath.replace(/\\/g, "/")}`);
  } finally {
    await rm(dir, { force: true, recursive: true });
  }
}

const live = await importTypescriptModule(new URL("../src/live/jobLiveStatus.ts", import.meta.url));

function job(overrides = {}) {
  return {
    job_id: "job_1",
    paper_name: "paper",
    status: "queued",
    execution_status: "queued",
    evaluation_status: "pending",
    quality_status: "pending",
    process_state: "none",
    cancelable: true,
    cancel_unavailable_reason: "",
    process_active: false,
    stage: "queued",
    current_stage: null,
    recovery_count: 0,
    recovery_status: "none",
    recovery_error_code: null,
    message: null,
    updated_at: "2026-09-03T00:00:00.000Z",
    repo_status: null,
    eval_score: null,
    run_dir: "Z:\\paper2code\\runs\\job_1",
    version: 1,
    cost_budget_policy: "none",
    cost_budget_currency: null,
    cost_budget_amount: null,
    cost_summary: null,
    ...overrides,
  };
}

function event(overrides = {}) {
  return {
    schema: "paper2code.job_event.v1",
    event_id: 1,
    job_id: "job_1",
    event_type: "job.status_changed",
    source: "worker",
    job_version: 2,
    execution_status: "running",
    evaluation_status: "pending",
    quality_status: "pending",
    created_at: "2026-09-03T00:00:01.000Z",
    payload: {},
    resync_required: false,
    ...overrides,
  };
}

function command(overrides = {}) {
  return {
    command_id: 1,
    job_id: "job_1",
    command_type: "cancel",
    status: "pending",
    request_status: "accepted",
    error_code: null,
    rejection_code: null,
    result_code: null,
    created_at: "2026-09-03T00:00:02.000Z",
    claimed_at: null,
    completed_at: null,
    updated_at: "2026-09-03T00:00:02.000Z",
    version: 1,
    ...overrides,
  };
}

test("applies SSE events once and ignores replayed duplicates", () => {
  let state = live.createInitialLiveJobState(job());
  state = live.liveJobReducer(state, { type: "sse_event", event: event() });
  const replayed = live.liveJobReducer(state, { type: "sse_event", event: event() });

  assert.equal(state.job.execution_status, "running");
  assert.equal(state.job.status, "running");
  assert.equal(replayed.appliedEventCount, 1);
  assert.deepEqual(replayed.seenEventIds, [1]);
});

test("gap events force REST resync and reconnect from the latest event id", () => {
  let state = live.createInitialLiveJobState(job({ status: "running", execution_status: "running" }));
  state = live.liveJobReducer(state, {
    type: "stream_gap",
    gap: {
      schema: "paper2code.stream_control.v1",
      job_id: "job_1",
      last_event_id: 4,
      replay_limit: 2,
      available_event_count: 9,
      latest_event_id: 13,
      resync_required: true,
      reason: "replay_limit_exceeded",
    },
  });
  assert.equal(state.resyncRequired, true);
  assert.equal(state.connectionStatus, "resyncing");

  state = live.liveJobReducer(state, {
    type: "rest_snapshot",
    job: job({
      status: "completed",
      execution_status: "completed",
      evaluation_status: "completed",
      quality_status: "accepted",
      version: 7,
    }),
    commands: [],
    cursorEventId: 13,
  });

  assert.equal(state.resyncRequired, false);
  assert.equal(state.lastEventId, 13);
  assert.equal(state.job.quality_status, "accepted");
  assert.equal(live.makeJobEventsUrl("job_1", state.lastEventId), "/api/v1/jobs/job_1/events?replay_limit=100&eventsource=1&last_event_id=13");
});

test("connection errors use bounded reconnects before low-frequency REST fallback", () => {
  let state = live.createInitialLiveJobState(job({ status: "running", execution_status: "running" }));
  state = live.liveJobReducer(state, { type: "connection_error", maxReconnectAttempts: 3 });
  assert.equal(state.connectionStatus, "reconnecting");
  assert.equal(state.fallbackActive, false);
  assert.equal(live.nextReconnectDelayMs(state.reconnectAttempts), 2000);

  state = live.liveJobReducer(state, { type: "connection_error", maxReconnectAttempts: 3 });
  state = live.liveJobReducer(state, { type: "connection_error", maxReconnectAttempts: 3 });

  assert.equal(state.connectionStatus, "fallback");
  assert.equal(state.fallbackActive, true);
  assert.equal(live.REST_FALLBACK_INTERVAL_MS >= 10000, true);
});

test("persisted commands drive pending, applied, and rejected action states", () => {
  let state = live.createInitialLiveJobState(job({ status: "running", execution_status: "running" }));
  state = live.liveJobReducer(state, { type: "command_submitted", commandType: "cancel" });
  assert.equal(state.actions.cancel.phase, "pending");
  assert.equal(live.deriveActionAvailability(state.job, state.actions).cancel.enabled, false);

  state = live.liveJobReducer(state, {
    type: "command_result",
    command: command({ status: "completed", result_code: "canceled", completed_at: "2026-09-03T00:00:03.000Z" }),
  });
  assert.equal(state.actions.cancel.phase, "applied");

  state = live.liveJobReducer(state, {
    type: "command_result",
    command: command({
      command_id: 2,
      command_type: "retry",
      status: "rejected",
      request_status: "rejected",
      error_code: "invalid_state",
      rejection_code: "invalid_state",
      result_code: "invalid_state",
    }),
  });
  assert.equal(state.actions.retry.phase, "rejected");
  assert.equal(state.actions.retry.code, "invalid_state");
});

test("legal action availability follows execution, evaluation, quality, and recovery state", () => {
  const rejected = job({
    status: "completed",
    execution_status: "completed",
    evaluation_status: "completed",
    quality_status: "rejected",
    cancelable: false,
  });
  const failed = job({
    status: "failed",
    execution_status: "failed",
    evaluation_status: "skipped",
    quality_status: "skipped",
    cancelable: false,
  });

  assert.equal(live.deriveActionAvailability(rejected, live.createEmptyActionMap()).approve.enabled, true);
  assert.equal(live.deriveActionAvailability(rejected, live.createEmptyActionMap()).repair.enabled, true);
  assert.equal(live.deriveActionAvailability(failed, live.createEmptyActionMap()).retry.enabled, true);
  assert.equal(live.deriveActionAvailability(failed, live.createEmptyActionMap()).cancel.enabled, false);
});

test("cost formatting preserves unknown attempts and separate currencies", () => {
  const lines = live.formatCostSummary({
    attempt_count: 4,
    status_counts: { actual: 1, estimated: 1, unknown: 2 },
    actual_by_currency: { USD: "1.20" },
    estimated_by_currency: { EUR: "0.40" },
    reserved_by_currency: {},
    unknown_attempts: 2,
    budget_policy: "hard",
    budget_currency: "USD",
    budget_amount: "5.00",
  });

  assert.deepEqual(lines.amounts, ["Actual USD 1.20", "Estimated EUR 0.40"]);
  assert.equal(lines.unknown, "Unknown attempts 2");
  assert.equal(lines.budget, "Hard budget USD 5.00");
  assert.equal(lines.amounts.some((line) => line.includes("Total 0")), false);
});

test("error sanitizer hides credentials, prompts, responses, and local paths", () => {
  const sanitized = live.sanitizeApiError({
    status: 500,
    code: "provider_api_key_failed",
    message: "API key failed for prompt at C:\\private\\secret with full response text.",
    details: { path: "Z:\\paper2code\\runs\\x" },
  });

  const rendered = `${sanitized.code} ${sanitized.message} ${JSON.stringify(sanitized.details)}`.toLowerCase();
  assert.equal(rendered.includes("api key"), false);
  assert.equal(rendered.includes("prompt"), false);
  assert.equal(rendered.includes("response"), false);
  assert.equal(rendered.includes("c:\\private"), false);
  assert.equal(rendered.includes("z:\\paper2code"), false);
});
