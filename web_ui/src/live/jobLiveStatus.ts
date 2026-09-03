import type { ApiError, CostSummary, JobCommand, JobCommandType, JobDetail } from "../api/types";

export const JOB_EVENT_SCHEMA = "paper2code.job_event.v1";
export const STREAM_CONTROL_SCHEMA = "paper2code.stream_control.v1";
export const DEFAULT_REPLAY_LIMIT = 100;
export const MAX_RECONNECT_ATTEMPTS = 3;
export const REST_FALLBACK_INTERVAL_MS = 15000;

export const JOB_EVENT_TYPES = [
  "job.created",
  "job.status_changed",
  "job.command_requested",
  "job.command_rejected",
  "job.command_failed",
  "job.retry_queued",
  "job.approved",
  "job.repair_requested",
  "job.process_completed",
  "job.process_failed",
  "job.canceled",
  "job.recovery_prepared",
] as const;

export type LiveConnectionStatus =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "resyncing"
  | "fallback"
  | "closed";

export type CommandActionPhase = "idle" | "pending" | "applied" | "rejected";

export type CommandActionState = {
  phase: CommandActionPhase;
  commandId: number | null;
  code: string | null;
  updatedAt: string | null;
};

export type ActionAvailability = {
  enabled: boolean;
  reason: string;
};

export type JobEventPayload = {
  command_id?: unknown;
  command_type?: unknown;
  command_status?: unknown;
  reason?: unknown;
  current_stage?: unknown;
  stage?: unknown;
  recovery_status?: unknown;
  recovery_error_code?: unknown;
};

export type JobSseEvent = {
  schema: string;
  event_id: number;
  job_id: string;
  event_type: string;
  source: string;
  job_version: number;
  execution_status: string | null;
  evaluation_status: string | null;
  quality_status: string | null;
  created_at: string;
  payload?: JobEventPayload | null;
  resync_required: boolean;
};

export type StreamGapEvent = {
  schema: string;
  job_id: string;
  last_event_id: number;
  replay_limit: number;
  available_event_count: number;
  latest_event_id: number | null;
  resync_required: boolean;
  reason: string;
};

export type LiveJobState = {
  job: JobDetail | null;
  actions: Record<JobCommandType, CommandActionState>;
  connectionStatus: LiveConnectionStatus;
  lastEventId: number;
  seenEventIds: number[];
  appliedEventCount: number;
  reconnectAttempts: number;
  fallbackActive: boolean;
  resyncRequired: boolean;
  lastError: ApiError | null;
};

export type LiveJobReducerAction =
  | { type: "rest_snapshot"; job: JobDetail; commands?: JobCommand[]; cursorEventId?: number | null }
  | { type: "sse_event"; event: JobSseEvent }
  | { type: "stream_gap"; gap: StreamGapEvent }
  | { type: "connection_open" }
  | { type: "connection_closed" }
  | { type: "connection_connecting" }
  | { type: "connection_error"; error?: ApiError | null; maxReconnectAttempts?: number }
  | { type: "fallback_poll" }
  | { type: "command_submitted"; commandType: JobCommandType }
  | { type: "command_result"; command: JobCommand }
  | { type: "command_error"; commandType: JobCommandType; error: ApiError };

const COMMAND_TYPES: JobCommandType[] = ["approve", "cancel", "retry", "repair"];
const TERMINAL_EXECUTION_STATUSES = new Set(["completed", "failed", "canceled"]);
const SENSITIVE_ERROR_PATTERN =
  /api[\s_-]*key|authorization|bearer|prompt|full\s+response|complete\s+response|model_response|base_url|[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/]/i;

export function createEmptyActionMap(): Record<JobCommandType, CommandActionState> {
  return {
    approve: createActionState(),
    cancel: createActionState(),
    retry: createActionState(),
    repair: createActionState(),
  };
}

export function createInitialLiveJobState(job: JobDetail | null = null): LiveJobState {
  return {
    job,
    actions: createEmptyActionMap(),
    connectionStatus: "idle",
    lastEventId: 0,
    seenEventIds: [],
    appliedEventCount: 0,
    reconnectAttempts: 0,
    fallbackActive: false,
    resyncRequired: false,
    lastError: null,
  };
}

export function liveJobReducer(state: LiveJobState, action: LiveJobReducerAction): LiveJobState {
  switch (action.type) {
    case "rest_snapshot": {
      const cursorEventId =
        typeof action.cursorEventId === "number" && Number.isInteger(action.cursorEventId)
          ? Math.max(0, action.cursorEventId)
          : state.lastEventId;
      return {
        ...state,
        job: normalizeJob(action.job),
        actions: mergeCommandsIntoActions(state.actions, action.commands || []),
        lastEventId: Math.max(state.lastEventId, cursorEventId),
        resyncRequired: false,
        lastError: null,
      };
    }
    case "sse_event": {
      if (!isUsableJobEvent(action.event, state.job?.job_id || action.event.job_id)) {
        return state;
      }
      if (action.event.event_id <= state.lastEventId || state.seenEventIds.includes(action.event.event_id)) {
        return state;
      }
      const nextJob = applyEventToJob(state.job, action.event);
      return {
        ...state,
        job: nextJob,
        actions: applyEventToActions(state.actions, action.event),
        connectionStatus: "open",
        lastEventId: action.event.event_id,
        seenEventIds: rememberEventId(state.seenEventIds, action.event.event_id),
        appliedEventCount: state.appliedEventCount + 1,
        reconnectAttempts: 0,
        fallbackActive: false,
        resyncRequired: Boolean(action.event.resync_required),
        lastError: null,
      };
    }
    case "stream_gap": {
      return {
        ...state,
        connectionStatus: "resyncing",
        lastEventId: Math.max(state.lastEventId, action.gap.latest_event_id || action.gap.last_event_id || 0),
        fallbackActive: false,
        resyncRequired: true,
        lastError: null,
      };
    }
    case "connection_connecting":
      return {
        ...state,
        connectionStatus: state.reconnectAttempts > 0 ? "reconnecting" : "connecting",
        fallbackActive: false,
      };
    case "connection_open":
      return {
        ...state,
        connectionStatus: "open",
        reconnectAttempts: 0,
        fallbackActive: false,
        lastError: null,
      };
    case "connection_closed":
      return {
        ...state,
        connectionStatus: "closed",
        fallbackActive: false,
      };
    case "connection_error": {
      const attempts = state.reconnectAttempts + 1;
      const maxAttempts = action.maxReconnectAttempts ?? MAX_RECONNECT_ATTEMPTS;
      const fallbackActive = attempts >= maxAttempts;
      return {
        ...state,
        connectionStatus: fallbackActive ? "fallback" : "reconnecting",
        reconnectAttempts: attempts,
        fallbackActive,
        lastError: action.error ? sanitizeApiError(action.error) : state.lastError,
      };
    }
    case "fallback_poll":
      return {
        ...state,
        connectionStatus: "fallback",
        fallbackActive: true,
      };
    case "command_submitted":
      return {
        ...state,
        actions: {
          ...state.actions,
          [action.commandType]: {
            phase: "pending",
            commandId: null,
            code: null,
            updatedAt: new Date().toISOString(),
          },
        },
        lastError: null,
      };
    case "command_result":
      return {
        ...state,
        actions: mergeCommandsIntoActions(state.actions, [action.command]),
        lastError: null,
      };
    case "command_error":
      return {
        ...state,
        actions: {
          ...state.actions,
          [action.commandType]: {
            phase: "rejected",
            commandId: null,
            code: action.error.code,
            updatedAt: new Date().toISOString(),
          },
        },
        lastError: sanitizeApiError(action.error),
      };
    default:
      return state;
  }
}

export function deriveActionAvailability(
  job: JobDetail | null,
  actions: Record<JobCommandType, CommandActionState>,
): Record<JobCommandType, ActionAvailability> {
  const blockedByPending = COMMAND_TYPES.some((commandType) => actions[commandType].phase === "pending");
  const pendingReason = blockedByPending ? "another_command_pending" : "";
  const executionStatus = currentExecutionStatus(job);
  const evaluationStatus = job?.evaluation_status || "";
  const qualityStatus = job?.quality_status || "";
  const identityUnresolved =
    job?.cancel_unavailable_reason === "process_identity_unresolved" || job?.process_state === "detached";
  const canCancel =
    Boolean(job?.cancelable) &&
    !identityUnresolved &&
    (executionStatus === "queued" || executionStatus === "running");
  const canRetry = executionStatus === "failed" || executionStatus === "canceled";
  const canResolveQuality =
    executionStatus === "completed" && evaluationStatus === "completed" && qualityStatus === "rejected";

  return {
    approve: availability(canResolveQuality, blockedByPending, pendingReason || "quality_not_rejected"),
    cancel: availability(canCancel, blockedByPending, pendingReason || job?.cancel_unavailable_reason || "not_cancelable"),
    retry: availability(canRetry, blockedByPending, pendingReason || "not_failed_or_canceled"),
    repair: availability(canResolveQuality, blockedByPending, pendingReason || "quality_not_rejected"),
  };
}

export function isTerminalJob(job: JobDetail | null): boolean {
  return TERMINAL_EXECUTION_STATUSES.has(currentExecutionStatus(job));
}

export function makeJobEventsUrl(
  jobId: string,
  lastEventId = 0,
  apiBaseUrl = "",
  replayLimit = DEFAULT_REPLAY_LIMIT,
): string {
  const params = new URLSearchParams({
    replay_limit: String(replayLimit),
    eventsource: "1",
  });
  if (lastEventId > 0) {
    params.set("last_event_id", String(lastEventId));
  }
  const base = apiBaseUrl.replace(/\/+$/, "");
  return `${base}/api/v1/jobs/${encodeURIComponent(jobId)}/events?${params.toString()}`;
}

export function nextReconnectDelayMs(attempts: number): number {
  const safeAttempts = Math.max(0, Math.min(3, Math.trunc(attempts)));
  return Math.min(8000, 1000 * 2 ** safeAttempts);
}

export function formatCostSummary(summary: CostSummary | null | undefined): {
  amounts: string[];
  unknown: string;
  budget: string;
  statusCounts: string[];
} {
  if (!summary || summary.attempt_count === 0) {
    return {
      amounts: ["No remote-call costs recorded"],
      unknown: "Unknown attempts 0",
      budget: "No hard budget",
      statusCounts: [],
    };
  }
  const amounts = [
    ...formatCurrencyMap("Actual", summary.actual_by_currency),
    ...formatCurrencyMap("Estimated", summary.estimated_by_currency),
    ...formatCurrencyMap("Reserved", summary.reserved_by_currency),
  ];
  return {
    amounts: amounts.length > 0 ? amounts : ["No known currency amounts"],
    unknown: `Unknown attempts ${summary.unknown_attempts}`,
    budget:
      summary.budget_policy === "hard"
        ? `Hard budget ${summary.budget_currency || "unknown currency"} ${summary.budget_amount || "unknown amount"}`
        : "No hard budget",
    statusCounts: Object.entries(summary.status_counts || {})
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([status, count]) => `${status} ${count}`),
  };
}

export function deriveRepairRecoveryStatus(job: JobDetail | null): string {
  if (!job) {
    return "unknown";
  }
  if (
    currentExecutionStatus(job) === "completed" &&
    job.evaluation_status === "completed" &&
    job.quality_status === "rejected"
  ) {
    return "repair_available";
  }
  const recoveryStatus = job.recovery_status || "none";
  if (recoveryStatus !== "none") {
    return `recovery_${recoveryStatus}`;
  }
  if (job.quality_status === "accepted") {
    return "no_repair_needed";
  }
  if (job.evaluation_status === "failed") {
    return "repair_blocked_evaluation_failed";
  }
  return "none";
}

export function sanitizeApiError(error: ApiError): ApiError {
  const joined = `${error.code} ${error.message} ${JSON.stringify(error.details || {})}`;
  if (!SENSITIVE_ERROR_PATTERN.test(joined)) {
    return error;
  }
  return {
    status: error.status,
    code: SENSITIVE_ERROR_PATTERN.test(error.code) ? "request_failed" : error.code,
    message: "Request failed. Sensitive details were hidden.",
    details: {},
  };
}

function createActionState(): CommandActionState {
  return {
    phase: "idle",
    commandId: null,
    code: null,
    updatedAt: null,
  };
}

function normalizeJob(job: JobDetail): JobDetail {
  const executionStatus = job.execution_status || job.status || "unknown";
  return {
    ...job,
    status: executionStatus,
    execution_status: executionStatus,
  };
}

function currentExecutionStatus(job: JobDetail | null): string {
  return job?.execution_status || job?.status || "";
}

function rememberEventId(eventIds: number[], eventId: number): number[] {
  const next = [...eventIds, eventId];
  return next.length > 500 ? next.slice(next.length - 500) : next;
}

function isUsableJobEvent(event: JobSseEvent, expectedJobId: string): boolean {
  return (
    event.schema === JOB_EVENT_SCHEMA &&
    event.job_id === expectedJobId &&
    Number.isInteger(event.event_id) &&
    event.event_id > 0 &&
    Number.isInteger(event.job_version) &&
    event.job_version > 0
  );
}

function applyEventToJob(job: JobDetail | null, event: JobSseEvent): JobDetail | null {
  if (!job) {
    return job;
  }
  const payload = event.payload || {};
  const executionStatus = event.execution_status || currentExecutionStatus(job);
  const currentStage = stringPayload(payload.current_stage) || stringPayload(payload.stage) || job.current_stage || null;
  const recoveryStatus = stringPayload(payload.recovery_status) || job.recovery_status;
  return {
    ...job,
    status: executionStatus,
    execution_status: executionStatus,
    evaluation_status: event.evaluation_status || job.evaluation_status,
    quality_status: event.quality_status || job.quality_status,
    version: Math.max(Number(job.version || 0), event.job_version),
    updated_at: event.created_at || job.updated_at,
    current_stage: currentStage,
    stage: currentStage || executionStatus || job.stage,
    recovery_status: recoveryStatus,
    recovery_error_code: stringPayload(payload.recovery_error_code) || job.recovery_error_code,
  };
}

function applyEventToActions(
  actions: Record<JobCommandType, CommandActionState>,
  event: JobSseEvent,
): Record<JobCommandType, CommandActionState> {
  const payload = event.payload || {};
  const commandType = stringPayload(payload.command_type);
  if (!isJobCommandType(commandType)) {
    return actions;
  }
  const commandId = numberPayload(payload.command_id);
  const commandStatus = stringPayload(payload.command_status);
  const reason = stringPayload(payload.reason);
  if (!commandId && !commandStatus && !reason) {
    return actions;
  }
  let phase: CommandActionPhase = "pending";
  if (commandStatus === "completed" || event.event_type === "job.approved" || event.event_type === "job.retry_queued") {
    phase = "applied";
  } else if (
    commandStatus === "rejected" ||
    commandStatus === "failed" ||
    event.event_type === "job.command_rejected" ||
    event.event_type === "job.command_failed"
  ) {
    phase = "rejected";
  }
  return {
    ...actions,
    [commandType]: {
      phase,
      commandId: commandId || actions[commandType].commandId,
      code: phase === "rejected" ? reason || "command_rejected" : null,
      updatedAt: event.created_at || actions[commandType].updatedAt,
    },
  };
}

function mergeCommandsIntoActions(
  actions: Record<JobCommandType, CommandActionState>,
  commands: JobCommand[],
): Record<JobCommandType, CommandActionState> {
  if (commands.length === 0) {
    return actions;
  }
  const next = { ...actions };
  for (const command of commands) {
    if (!isJobCommandType(command.command_type)) {
      continue;
    }
    const existing = next[command.command_type];
    if (existing.commandId !== null && existing.commandId > command.command_id) {
      continue;
    }
    next[command.command_type] = actionStateFromCommand(command);
  }
  return next;
}

function actionStateFromCommand(command: JobCommand): CommandActionState {
  let phase: CommandActionPhase = "pending";
  if (command.status === "completed") {
    phase = "applied";
  } else if (command.status === "rejected" || command.status === "failed" || command.request_status === "rejected") {
    phase = "rejected";
  }
  return {
    phase,
    commandId: command.command_id,
    code:
      phase === "rejected"
        ? command.rejection_code || command.error_code || command.result_code || "command_rejected"
        : command.result_code,
    updatedAt: command.updated_at || command.completed_at || command.created_at,
  };
}

function isJobCommandType(value: unknown): value is JobCommandType {
  return typeof value === "string" && COMMAND_TYPES.includes(value as JobCommandType);
}

function stringPayload(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function numberPayload(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}

function availability(enabledByState: boolean, blockedByPending: boolean, reason: string): ActionAvailability {
  if (blockedByPending) {
    return { enabled: false, reason };
  }
  return enabledByState ? { enabled: true, reason: "" } : { enabled: false, reason };
}

function formatCurrencyMap(label: string, values: Record<string, string>): string[] {
  return Object.entries(values || {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([currency, amount]) => `${label} ${currency} ${amount}`);
}
