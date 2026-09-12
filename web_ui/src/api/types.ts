export type ApiError = {
  status: number;
  code: string;
  message: string;
  details: Record<string, unknown>;
};

export type ProviderName = string;
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

export type ProviderSettingsView = ProviderSettingsStatus & {
  provider: ProviderName;
  model: string;
  base_url: string;
};

export type EvaluationSettingsView = ProviderSettingsView & {
  fallback_models: string[];
};

export type SettingsView = {
  configured: boolean;
  reproduce: ProviderSettingsView;
  evaluation: EvaluationSettingsView;
};

export type ProviderModelDiscovery = {
  model_id: string;
  max_n: number;
  context_window: number | null;
  max_output_tokens: number | null;
  json_schema_support: boolean | null;
  usage_support: boolean | null;
  cache_token_support: boolean | null;
};

export type ProviderDiscovery = {
  provider_id: ProviderName;
  models: ProviderModelDiscovery[];
};

export type ProviderRegistryResponse = {
  registry_version: number;
  providers: ProviderDiscovery[];
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

export type CostBudgetPolicy = "none" | "hard";

export type JobCommandType = "approve" | "cancel" | "retry" | "repair";
export type JobCommandStatus = "pending" | "claimed" | "completed" | "failed" | "rejected";
export type JobCommandRequestStatus = "accepted" | "rejected";

export type CostSummary = {
  attempt_count: number;
  status_counts: Record<string, number>;
  actual_by_currency: Record<string, string>;
  estimated_by_currency: Record<string, string>;
  reserved_by_currency: Record<string, string>;
  unknown_attempts: number;
  budget_policy: CostBudgetPolicy;
  budget_currency: string | null;
  budget_amount: string | null;
};

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
  current_stage?: string | null;
  stage_attempt?: number | null;
  last_checkpoint_stage?: string | null;
  recovery_count?: number;
  recovery_status?: "none" | "prepared" | "running" | "completed" | "failed" | string;
  recovery_error_code?: string | null;
  message: string | null;
  updated_at: string | null;
  repo_status: string | null;
  eval_score: number | null;
  run_dir: string;
  execution_status?: string | null;
  evaluation_status?: string | null;
  quality_status?: string | null;
  failure_code?: string | null;
  version?: number | null;
  reproduce_provider?: string | null;
  reproduce_model?: string | null;
  evaluation_provider?: string | null;
  evaluation_model?: string | null;
  evaluation_fallback_models?: string[] | null;
  provider_registry_version?: number | null;
  provider_contract_fingerprint?: string | null;
  cost_budget_policy?: CostBudgetPolicy | null;
  cost_budget_currency?: string | null;
  cost_budget_amount?: string | null;
  cost_summary?: CostSummary | null;
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
  cost_budget_policy?: CostBudgetPolicy | null;
  cost_budget_currency?: string | null;
  cost_budget_amount?: string | null;
  cost_summary?: CostSummary | null;
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
  cost_budget_policy?: CostBudgetPolicy;
  cost_budget_currency?: string | null;
  cost_budget_amount?: string | null;
};

export type CancelResponse = {
  job_id: string;
  canceled: boolean;
  message: string;
};

export type JobCommand = {
  command_id: number;
  job_id: string;
  command_type: JobCommandType;
  status: JobCommandStatus;
  request_status: JobCommandRequestStatus;
  error_code: string | null;
  rejection_code: string | null;
  result_code: string | null;
  created_at: string;
  claimed_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
  version: number;
};

export type JobCommandsResponse = {
  commands: JobCommand[];
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
