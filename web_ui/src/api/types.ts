export type ApiError = {
  status: number;
  code: string;
  message: string;
  details: Record<string, unknown>;
};

export type ProviderName = "deepseek" | "kimi" | "qwen" | "claude" | "openai";
export type DomainName = "general" | "statistics";
export type EvalType = "ref_free";
export type ConsoleOutput = "progress" | "full" | "quiet";

export type ProviderSettingsStatus = {
  has_api_key: boolean;
};

export type EvaluationSettingsStatus = ProviderSettingsStatus;

export type SettingsStatus = {
  configured: boolean;
  reproduce: ProviderSettingsStatus;
  evaluation: EvaluationSettingsStatus;
};

export type WebSettingsPayload = {
  reproduce: {
    provider: ProviderName;
    model: string;
    api_key: string;
    base_url: string;
  };
  evaluation: {
    provider: ProviderName;
    model: string;
    api_key: string;
    base_url: string;
    fallback_models: string[];
  };
};

export type JobStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "canceled"
  | "unknown"
  | string;

export type ProcessState =
  | "active"
  | "finished"
  | "detached"
  | "orphaned"
  | "none"
  | string;

export type JobListItem = {
  job_id: string;
  paper_name: string | null;
  status: JobStatus;
  process_state: ProcessState;
  cancelable: boolean;
  cancel_unavailable_reason: string;
  process_active: boolean;
  stage: string | null;
  message: string | null;
  updated_at: string | null;
  repo_status: string | null;
  eval_score: number | null;
  run_dir: string;
};

export type JobDetail = JobListItem & {
  started_at?: string;
  pid?: number;
  process_pid?: number;
  process_group_id?: number;
  process_kill_strategy?: string;
  return_code?: number | null;
  launcher_log?: string;
  state_file_error?: {
    file_name: string;
    error: string;
  };
};

export type JobCreateResponse = {
  job_id: string;
  status: string;
  run_dir: string;
  status_path: string;
  summary_path: string;
};

export type UploadResponse = {
  upload_id: string;
  size: number;
};

export type JobCreatePayload = {
  upload_id: string;
  paper_name: string;
  domain: DomainName;
  eval_type: EvalType;
  generated_n: number;
  auto_refine: boolean;
  max_repair_rounds: number;
  console_output: ConsoleOutput;
  skip_mineru: boolean;
  pdf_markdown_path: string;
};

export type CancelResponse = {
  job_id: string;
  canceled: boolean;
  message: string;
};

export type LogsResponse = {
  job_id: string;
  logs: string[];
  file: string | null;
  content: string | null;
};

export type ArtifactSummary = {
  job_id: string;
  run_dir: string;
  repo_dir: string;
  results_dir: string;
  logs_dir: string;
  repo_file_count: number;
  result_file_count: number;
  log_file_count: number;
  repo_ready: boolean;
};

export type RepoFileEntry = {
  path: string;
  name: string;
  type: "file" | "directory";
  size: number | null;
  modified_at: number;
};

export type RepoTreeResponse = {
  job_id: string;
  files: RepoFileEntry[];
};

export type RepoFileResponse = {
  job_id: string;
  path: string;
  size: number;
  content: string;
};
