import {
  CheckCircle2,
  Download,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Square,
  Wrench,
} from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { API_BASE_URL, api, toApiError } from "../api/client";
import type {
  ApiError,
  ArtifactSummary,
  JobCommand,
  JobCommandType,
  JobDetail,
  LogsResponse,
  RepoFileResponse,
  RepoTreeResponse,
} from "../api/types";
import ErrorNotice from "../components/ErrorNotice";
import FileTree from "../components/FileTree";
import LogViewer from "../components/LogViewer";
import StatusBadge from "../components/StatusBadge";
import {
  JOB_EVENT_TYPES,
  MAX_RECONNECT_ATTEMPTS,
  REST_FALLBACK_INTERVAL_MS,
  createInitialLiveJobState,
  deriveActionAvailability,
  deriveRepairRecoveryStatus,
  formatCostSummary,
  isTerminalJob,
  liveJobReducer,
  makeJobEventsUrl,
  nextReconnectDelayMs,
  sanitizeApiError,
  type JobSseEvent,
  type StreamGapEvent,
} from "../live/jobLiveStatus";

type DetailTab = "artifacts" | "logs" | "repo";

export default function JobDetailPage() {
  const { jobId = "" } = useParams();
  const [liveState, dispatchLive] = useReducer(
    liveJobReducer,
    null,
    () => createInitialLiveJobState(null),
  );
  const liveStateRef = useRef(liveState);
  const [artifacts, setArtifacts] = useState<ArtifactSummary | null>(null);
  const [logs, setLogs] = useState<LogsResponse>({
    job_id: jobId,
    logs: [],
    file: null,
    content: null,
  });
  const [tree, setTree] = useState<RepoTreeResponse>({ job_id: jobId, files: [] });
  const [selectedRepoFile, setSelectedRepoFile] = useState<RepoFileResponse | null>(null);
  const [selectedLog, setSelectedLog] = useState<string | null>(null);
  const [tab, setTab] = useState<DetailTab>("artifacts");
  const [loadingLog, setLoadingLog] = useState(false);
  const [loadingRepoFile, setLoadingRepoFile] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [resourceErrors, setResourceErrors] = useState<ApiError[]>([]);
  const [cancelMessage, setCancelMessage] = useState("");
  const [commandsAvailable, setCommandsAvailable] = useState(true);

  useEffect(() => {
    liveStateRef.current = liveState;
  }, [liveState]);

  const loadJobSnapshot = useCallback(
    async (
      cursorEventId?: number | null,
      options: { clearError?: boolean } = {},
    ): Promise<JobDetail | null> => {
      if (!jobId) return null;
      const results = await Promise.allSettled([api.getJob(jobId), api.listJobCommands(jobId)]);
      if (results[0].status === "rejected") {
        const safeError = sanitizeApiError(toApiError(results[0].reason));
        setError(safeError);
        return null;
      }
      let commandError: ApiError | null = null;
      const commands = commandListFromResult(
        results[1],
        (nextError) => {
          commandError = nextError;
        },
        setCommandsAvailable,
      );
      dispatchLive({
        type: "rest_snapshot",
        job: results[0].value,
        commands,
        cursorEventId,
      });
      if (commandError) {
        setError(commandError);
      } else if (options.clearError ?? true) {
        setError(null);
      }
      return results[0].value;
    },
    [jobId],
  );

  const loadResources = useCallback(async () => {
    if (!jobId) return;
    const nextResourceErrors: ApiError[] = [];
    const [artifactResult, logsResult, treeResult] = await Promise.allSettled([
      api.getArtifacts(jobId),
      api.getLogs(jobId, selectedLog || undefined),
      api.getRepoTree(jobId),
    ]);

    if (artifactResult.status === "fulfilled") {
      setArtifacts(artifactResult.value);
    } else {
      setArtifacts(null);
      nextResourceErrors.push(sanitizeApiError(toApiError(artifactResult.reason)));
    }

    if (logsResult.status === "fulfilled") {
      setLogs(logsResult.value);
    } else {
      setLogs({ job_id: jobId, logs: [], file: null, content: null });
      nextResourceErrors.push(sanitizeApiError(toApiError(logsResult.reason)));
    }

    if (treeResult.status === "fulfilled") {
      setTree(treeResult.value);
    } else {
      setTree({ job_id: jobId, files: [] });
      nextResourceErrors.push(sanitizeApiError(toApiError(treeResult.reason)));
    }
    setResourceErrors(nextResourceErrors);
  }, [jobId, selectedLog]);

  useEffect(() => {
    void loadResources();
  }, [loadResources]);

  useEffect(() => {
    if (!jobId) return undefined;

    let disposed = false;
    let source: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let fallbackTimer: number | null = null;
    let snapshotTimer: number | null = null;
    let reconnectAttempts = 0;

    const clearTimer = (timer: number | null) => {
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };

    const closeSource = () => {
      if (source) {
        source.close();
        source = null;
      }
    };

    const stopFallback = () => {
      if (fallbackTimer !== null) {
        window.clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
    };

    const saveCursor = (eventId: number) => {
      if (eventId > 0) {
        writeStoredCursor(jobId, eventId);
      }
    };

    const scheduleSnapshot = (cursorEventId: number) => {
      clearTimer(snapshotTimer);
      snapshotTimer = window.setTimeout(() => {
        snapshotTimer = null;
        void loadJobSnapshot(cursorEventId).then((snapshot) => {
          if (disposed || !snapshot) return;
          if (isTerminalJob(snapshot)) {
            closeSource();
            stopFallback();
            dispatchLive({ type: "connection_closed" });
          }
        });
      }, 250);
    };

    const startFallback = () => {
      closeSource();
      clearTimer(reconnectTimer);
      if (fallbackTimer !== null) return;
      dispatchLive({ type: "fallback_poll" });
      const poll = async () => {
        const snapshot = await loadJobSnapshot(readStoredCursor(jobId));
        if (disposed || !snapshot) return;
        if (isTerminalJob(snapshot)) {
          stopFallback();
          dispatchLive({ type: "connection_closed" });
        }
      };
      void poll();
      fallbackTimer = window.setInterval(poll, REST_FALLBACK_INTERVAL_MS);
    };

    const handleError = () => {
      if (disposed) return;
      closeSource();
      reconnectAttempts += 1;
      dispatchLive({
        type: "connection_error",
        maxReconnectAttempts: MAX_RECONNECT_ATTEMPTS,
      });
      if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
        startFallback();
        return;
      }
      reconnectTimer = window.setTimeout(
        () => connect(readStoredCursor(jobId)),
        nextReconnectDelayMs(reconnectAttempts),
      );
    };

    const handleJobEvent = (message: MessageEvent<string>) => {
      const event = parseSseData<JobSseEvent>(message.data);
      if (!event || event.job_id !== jobId) {
        setError(frontendError("event_parse_error", "A live job event could not be applied safely."));
        return;
      }
      saveCursor(event.event_id);
      dispatchLive({ type: "sse_event", event });
      scheduleSnapshot(event.event_id);
      if (event.resync_required) {
        dispatchLive({
          type: "stream_gap",
          gap: {
            schema: "paper2code.stream_control.v1",
            job_id: jobId,
            last_event_id: event.event_id,
            replay_limit: 0,
            available_event_count: 0,
            latest_event_id: event.event_id,
            resync_required: true,
            reason: "event_requested_resync",
          },
        });
      }
    };

    const handleGap = (message: MessageEvent<string>) => {
      const gap = parseSseData<StreamGapEvent>(message.data);
      if (!gap || gap.job_id !== jobId) {
        handleError();
        return;
      }
      closeSource();
      const cursor = gap.latest_event_id || readStoredCursor(jobId);
      saveCursor(cursor);
      dispatchLive({ type: "stream_gap", gap });
      void loadJobSnapshot(cursor).then((snapshot) => {
        if (disposed || !snapshot) return;
        if (isTerminalJob(snapshot)) {
          dispatchLive({ type: "connection_closed" });
          return;
        }
        reconnectAttempts = 0;
        connect(cursor);
      });
    };

    function connect(cursorEventId: number) {
      if (disposed) return;
      if (typeof window.EventSource === "undefined") {
        startFallback();
        return;
      }
      stopFallback();
      closeSource();
      dispatchLive({ type: "connection_connecting" });
      const nextSource = new EventSource(makeJobEventsUrl(jobId, cursorEventId, API_BASE_URL), {
        withCredentials: true,
      });
      source = nextSource;
      nextSource.onopen = () => {
        reconnectAttempts = 0;
        dispatchLive({ type: "connection_open" });
      };
      nextSource.onerror = handleError;
      for (const eventType of JOB_EVENT_TYPES) {
        nextSource.addEventListener(eventType, handleJobEvent);
      }
      nextSource.addEventListener("stream.gap", handleGap);
    }

    void loadJobSnapshot(readStoredCursor(jobId)).then((snapshot) => {
      if (disposed || !snapshot) return;
      if (isTerminalJob(snapshot)) {
        dispatchLive({ type: "connection_closed" });
        return;
      }
      connect(readStoredCursor(jobId));
    });

    return () => {
      disposed = true;
      closeSource();
      stopFallback();
      clearTimer(reconnectTimer);
      clearTimer(snapshotTimer);
    };
  }, [jobId, loadJobSnapshot]);

  const job = liveState.job;
  const actionAvailability = useMemo(() => {
    const availability = deriveActionAvailability(job, liveState.actions);
    if (commandsAvailable) {
      return availability;
    }
    return {
      ...availability,
      approve: { enabled: false, reason: "command_api_unavailable" },
      retry: { enabled: false, reason: "command_api_unavailable" },
      repair: { enabled: false, reason: "command_api_unavailable" },
    };
  }, [commandsAvailable, job, liveState.actions]);
  const cost = useMemo(() => formatCostSummary(job?.cost_summary), [job?.cost_summary]);
  const repairRecoveryStatus = useMemo(() => deriveRepairRecoveryStatus(job), [job]);
  const mainError = error || liveState.lastError;

  async function handleRefreshAll() {
    await loadJobSnapshot(liveStateRef.current.lastEventId);
    await loadResources();
  }

  async function handleCommand(commandType: JobCommandType) {
    if (!jobId || !actionAvailability[commandType].enabled) return;
    setCancelMessage("");
    setError(null);
    dispatchLive({ type: "command_submitted", commandType });
    try {
      if (!commandsAvailable && commandType === "cancel") {
        const response = await api.cancelJob(jobId);
        const now = new Date().toISOString();
        setCancelMessage(response.message);
        dispatchLive({
          type: "command_result",
          command: {
            command_id: 0,
            job_id: jobId,
            command_type: "cancel",
            status: response.canceled ? "completed" : "pending",
            request_status: "accepted",
            error_code: null,
            rejection_code: null,
            result_code: response.canceled ? "canceled" : "cancel_requested",
            created_at: now,
            claimed_at: null,
            completed_at: response.canceled ? now : null,
            updated_at: now,
            version: 1,
          },
        });
        await loadJobSnapshot(liveStateRef.current.lastEventId);
        return;
      }
      const command = await api.createJobCommand(jobId, commandType);
      dispatchLive({ type: "command_result", command });
      await loadJobSnapshot(liveStateRef.current.lastEventId);
    } catch (err) {
      const apiError = sanitizeApiError(toApiError(err));
      dispatchLive({ type: "command_error", commandType, error: apiError });
      setError(apiError);
      await loadJobSnapshot(liveStateRef.current.lastEventId, { clearError: false });
    }
  }

  async function handleSelectLog(file: string) {
    if (!jobId) return;
    setLoadingLog(true);
    setSelectedLog(file);
    setResourceErrors([]);
    try {
      const response = await api.getLogs(jobId, file);
      setLogs(response);
    } catch (err) {
      setResourceErrors([sanitizeApiError(toApiError(err))]);
    } finally {
      setLoadingLog(false);
    }
  }

  async function handleSelectRepoFile(path: string) {
    if (!jobId) return;
    setLoadingRepoFile(true);
    setResourceErrors([]);
    try {
      const response = await api.getRepoFile(jobId, path);
      setSelectedRepoFile(response);
    } catch (err) {
      setSelectedRepoFile(null);
      setResourceErrors([sanitizeApiError(toApiError(err))]);
    } finally {
      setLoadingRepoFile(false);
    }
  }

  async function handleDownloadRepo() {
    if (!jobId) return;
    setResourceErrors([]);
    try {
      const blob = await api.downloadRepo(jobId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${jobId}_repo.zip`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setResourceErrors([sanitizeApiError(toApiError(err))]);
    }
  }

  return (
    <section className="page-stack">
      <div className="page-header">
        <div>
          <h1>Job Detail</h1>
          <p className="mono-text">{jobId}</p>
        </div>
        <div className="header-actions">
          <button className="secondary-button" onClick={() => void handleRefreshAll()} type="button">
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </div>

      <ErrorNotice error={mainError} />
      {cancelMessage ? <div className="success-note">{cancelMessage}</div> : null}

      <div className="panel live-panel" aria-live="polite">
        <Metric label="live connection" value={<ConnectionLabel status={liveState.connectionStatus} />} />
        <Metric label="last_event_id" value={liveState.lastEventId || "-"} />
        <Metric label="resync" value={liveState.resyncRequired ? "required" : "ok"} />
        <Metric
          label="fallback"
          value={liveState.fallbackActive ? `REST every ${REST_FALLBACK_INTERVAL_MS / 1000}s` : "off"}
        />
      </div>

      <div className="panel detail-grid">
        <Metric
          label="execution status"
          value={<StatusBadge status={job?.execution_status || job?.status} processState={job?.process_state} />}
        />
        <Metric label="evaluation status" value={<StatusBadge status={job?.evaluation_status || "pending"} />} />
        <Metric label="quality status" value={<StatusBadge status={job?.quality_status || "pending"} />} />
        <Metric label="repair/recovery status" value={<StatusBadge status={repairRecoveryStatus} />} />
        <Metric label="stage" value={job?.stage || job?.current_stage || "-"} />
        <Metric label="version" value={job?.version ?? "-"} />
        <Metric label="failure_code" value={job?.failure_code || "-"} />
        <Metric label="message" value={job?.message || "-"} />
      </div>

      <div className="panel cost-panel">
        <div className="cost-section">
          <div className="metric-label">cost/budget status</div>
          <div className="cost-lines">
            {cost.amounts.map((line) => (
              <span key={line}>{line}</span>
            ))}
            <span>{cost.unknown}</span>
            <span>{cost.budget}</span>
          </div>
        </div>
        <div className="cost-section">
          <div className="metric-label">cost status counts</div>
          <div className="cost-lines">
            {cost.statusCounts.length > 0 ? (
              cost.statusCounts.map((line) => <span key={line}>{line}</span>)
            ) : (
              <span>No status counts</span>
            )}
          </div>
        </div>
      </div>

      <div className="panel command-panel" aria-live="polite">
        <JobCommandButton
          commandType="approve"
          icon={<ShieldCheck size={16} />}
          label="Approve"
          state={liveState.actions.approve}
          availability={actionAvailability.approve}
          onClick={handleCommand}
        />
        <JobCommandButton
          commandType="cancel"
          icon={<Square size={14} />}
          label="Cancel"
          state={liveState.actions.cancel}
          availability={actionAvailability.cancel}
          onClick={handleCommand}
          danger
        />
        <JobCommandButton
          commandType="retry"
          icon={<RotateCcw size={16} />}
          label="Retry"
          state={liveState.actions.retry}
          availability={actionAvailability.retry}
          onClick={handleCommand}
        />
        <JobCommandButton
          commandType="repair"
          icon={<Wrench size={16} />}
          label="Repair"
          state={liveState.actions.repair}
          availability={actionAvailability.repair}
          onClick={handleCommand}
        />
      </div>

      <div className="panel results-panel">
        <div className="tabs" role="tablist">
          {(["artifacts", "logs", "repo"] as DetailTab[]).map((item) => (
            <button
              aria-selected={tab === item}
              className={tab === item ? "active" : ""}
              key={item}
              onClick={() => setTab(item)}
              role="tab"
              type="button"
            >
              {item}
            </button>
          ))}
          <button className="download-button" onClick={handleDownloadRepo} type="button">
            <Download size={16} />
            Download Zip
          </button>
        </div>

        {resourceErrors.map((resourceError, index) => (
          <ErrorNotice error={resourceError} key={`${resourceError.code}-${index}`} />
        ))}

        {tab === "artifacts" ? (
          <div className="artifact-grid">
            {artifacts ? (
              <>
                <Metric label="repo_ready" value={String(artifacts.repo_ready)} />
                <Metric label="repo_file_count" value={artifacts.repo_file_count} />
                <Metric label="result_file_count" value={artifacts.result_file_count} />
                <Metric label="log_file_count" value={artifacts.log_file_count} />
              </>
            ) : (
              <div className="empty-state">Artifact summary is not available.</div>
            )}
          </div>
        ) : null}

        {tab === "logs" ? (
          <LogViewer
            logs={logs.logs}
            selectedLog={selectedLog}
            content={logs.content}
            loading={loadingLog}
            onSelectLog={handleSelectLog}
          />
        ) : null}

        {tab === "repo" ? (
          <div className="repo-browser">
            <FileTree
              files={tree.files}
              selectedPath={selectedRepoFile?.path || null}
              onSelectFile={handleSelectRepoFile}
            />
            <pre className="code-pane repo-content">
              {loadingRepoFile
                ? "Loading file..."
                : selectedRepoFile?.content || "Select a repository file."}
            </pre>
          </div>
        ) : null}
      </div>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
    </div>
  );
}

function ConnectionLabel({ status }: { status: string }) {
  return <StatusBadge status={status} />;
}

function JobCommandButton({
  commandType,
  icon,
  label,
  state,
  availability,
  onClick,
  danger,
}: {
  commandType: JobCommandType;
  icon: ReactNode;
  label: string;
  state: { phase: string; code: string | null };
  availability: { enabled: boolean; reason: string };
  onClick: (commandType: JobCommandType) => void;
  danger?: boolean;
}) {
  const disabled = !availability.enabled;
  const statusText = actionStatusText(state.phase, state.code);
  return (
    <button
      className={danger ? "danger-button command-button" : "secondary-button command-button"}
      disabled={disabled}
      onClick={() => onClick(commandType)}
      title={disabled && availability.reason ? availability.reason : `${label} job`}
      type="button"
    >
      {state.phase === "applied" ? <CheckCircle2 size={16} /> : icon}
      <span>{label}</span>
      <span className={`command-state command-state-${state.phase}`}>{statusText}</span>
    </button>
  );
}

function actionStatusText(phase: string, code: string | null): string {
  if (phase === "idle") return "idle";
  if (phase === "pending") return "pending";
  if (phase === "applied") return "applied";
  return code ? `rejected: ${code}` : "rejected";
}

function parseSseData<T>(data: string): T | null {
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}

function commandListFromResult(
  result: PromiseSettledResult<{ commands: JobCommand[] }>,
  setError: (error: ApiError | null) => void,
  setCommandsAvailable: (available: boolean) => void,
): JobCommand[] {
  if (result.status === "fulfilled") {
    setCommandsAvailable(true);
    return result.value.commands;
  }
  const apiError = sanitizeApiError(toApiError(result.reason));
  if (apiError.code === "feature_not_supported") {
    setCommandsAvailable(false);
  } else {
    setError(apiError);
  }
  return [];
}

function frontendError(code: string, message: string): ApiError {
  return {
    status: 0,
    code,
    message,
    details: {},
  };
}

function readStoredCursor(jobId: string): number {
  try {
    const raw = window.sessionStorage.getItem(cursorStorageKey(jobId));
    if (!raw) return 0;
    const parsed = Number(raw);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : 0;
  } catch {
    return 0;
  }
}

function writeStoredCursor(jobId: string, eventId: number) {
  try {
    window.sessionStorage.setItem(cursorStorageKey(jobId), String(eventId));
  } catch {
    // Cursor persistence is a convenience; REST remains authoritative.
  }
}

function cursorStorageKey(jobId: string): string {
  return `paper2code:last-event-id:${jobId}`;
}
