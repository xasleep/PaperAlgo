import { Download, RefreshCw, Square } from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, toApiError } from "../api/client";
import type {
  ApiError,
  ArtifactSummary,
  JobDetail,
  LogsResponse,
  RepoFileResponse,
  RepoTreeResponse,
} from "../api/types";
import ErrorNotice from "../components/ErrorNotice";
import FileTree from "../components/FileTree";
import LogViewer from "../components/LogViewer";
import StatusBadge from "../components/StatusBadge";

type DetailTab = "artifacts" | "logs" | "repo";
const TERMINAL_STATUSES = new Set(["completed", "failed", "canceled"]);

export default function JobDetailPage() {
  const { jobId = "" } = useParams();
  const [job, setJob] = useState<JobDetail | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactSummary | null>(null);
  const [logs, setLogs] = useState<LogsResponse>({ job_id: jobId, logs: [], file: null, content: null });
  const [tree, setTree] = useState<RepoTreeResponse>({ job_id: jobId, files: [] });
  const [selectedRepoFile, setSelectedRepoFile] = useState<RepoFileResponse | null>(null);
  const [selectedLog, setSelectedLog] = useState<string | null>(null);
  const [tab, setTab] = useState<DetailTab>("artifacts");
  const [loadingLog, setLoadingLog] = useState(false);
  const [loadingRepoFile, setLoadingRepoFile] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [resourceErrors, setResourceErrors] = useState<ApiError[]>([]);
  const [cancelMessage, setCancelMessage] = useState("");

  const loadJob = useCallback(async () => {
    if (!jobId) return;
    try {
      const nextJob = await api.getJob(jobId);
      setJob(nextJob);
      setError(null);
    } catch (err) {
      setError(toApiError(err));
    }
  }, [jobId]);

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
      nextResourceErrors.push(toApiError(artifactResult.reason));
    }

    if (logsResult.status === "fulfilled") {
      setLogs(logsResult.value);
    } else {
      setLogs({ job_id: jobId, logs: [], file: null, content: null });
      nextResourceErrors.push(toApiError(logsResult.reason));
    }

    if (treeResult.status === "fulfilled") {
      setTree(treeResult.value);
    } else {
      setTree({ job_id: jobId, files: [] });
      nextResourceErrors.push(toApiError(treeResult.reason));
    }
    setResourceErrors(nextResourceErrors);
  }, [jobId, selectedLog]);

  useEffect(() => {
    void loadJob();
    void loadResources();
  }, [loadJob, loadResources]);

  const autoRefresh = shouldAutoRefresh(job);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = window.setInterval(() => {
      void loadJob();
      void loadResources();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadJob, loadResources]);

  async function handleCancel() {
    if (!jobId) return;
    setCancelMessage("");
    setError(null);
    try {
      const response = await api.cancelJob(jobId);
      setCancelMessage(response.message);
      await loadJob();
    } catch (err) {
      setError(toApiError(err));
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
      setResourceErrors([toApiError(err)]);
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
      setResourceErrors([toApiError(err)]);
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
      setResourceErrors([toApiError(err)]);
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
          <button className="secondary-button" onClick={() => void loadResources()} type="button">
            <RefreshCw size={16} />
            Refresh Results
          </button>
          <button
            className="danger-button"
            disabled={!job?.cancelable}
            onClick={handleCancel}
            type="button"
          >
            <Square size={14} />
            Cancel
          </button>
        </div>
      </div>

      <ErrorNotice error={error} />
      {cancelMessage ? <div className="success-note">{cancelMessage}</div> : null}

      <div className="panel detail-grid">
        <Metric label="status" value={<StatusBadge status={job?.status} processState={job?.process_state} />} />
        <Metric label="cancelable" value={String(job?.cancelable ?? false)} />
        <Metric label="cancel_unavailable_reason" value={job?.cancel_unavailable_reason || "-"} />
        <Metric label="stage" value={job?.stage || "-"} />
        <Metric label="message" value={job?.message || "-"} />
        <Metric label="repo_status" value={job?.repo_status || "-"} />
        <Metric label="eval_score" value={job?.eval_score ?? "-"} />
        <Metric label="run_dir" value={<span className="debug-path">{job?.run_dir || "-"}</span>} />
      </div>

      <div className="panel results-panel">
        <div className="tabs" role="tablist">
          {(["artifacts", "logs", "repo"] as DetailTab[]).map((item) => (
            <button
              className={tab === item ? "active" : ""}
              key={item}
              onClick={() => setTab(item)}
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
                <Metric label="repo_dir" value={<span className="debug-path">{artifacts.repo_dir}</span>} />
                <Metric label="results_dir" value={<span className="debug-path">{artifacts.results_dir}</span>} />
                <Metric label="logs_dir" value={<span className="debug-path">{artifacts.logs_dir}</span>} />
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

function shouldAutoRefresh(job: JobDetail | null): boolean {
  if (!job || TERMINAL_STATUSES.has(String(job.status))) {
    return false;
  }
  return (
    job.cancelable ||
    job.status === "queued" ||
    job.status === "running" ||
    job.process_state === "active"
  );
}
