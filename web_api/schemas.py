from typing import Any, Literal

from pydantic import BaseModel, Field


ProviderName = Literal["deepseek", "kimi", "qwen", "claude", "openai"]
DomainName = Literal["general", "statistics"]
EvalType = Literal["ref_free", "ref_based"]
ConsoleOutput = Literal["progress", "full", "quiet"]


class ProviderSettings(BaseModel):
    provider: ProviderName
    model: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    base_url: str = ""


class EvaluationSettings(ProviderSettings):
    fallback_models: list[str] = Field(default_factory=list)


class WebSettings(BaseModel):
    reproduce: ProviderSettings
    evaluation: EvaluationSettings


class ProviderSettingsStatus(BaseModel):
    has_api_key: bool


class EvaluationSettingsStatus(ProviderSettingsStatus):
    pass


class SettingsStatus(BaseModel):
    configured: bool
    reproduce: ProviderSettingsStatus = Field(
        default_factory=lambda: ProviderSettingsStatus(has_api_key=False)
    )
    evaluation: EvaluationSettingsStatus = Field(
        default_factory=lambda: EvaluationSettingsStatus(has_api_key=False)
    )


class SessionResponse(BaseModel):
    csrf_token: str


class UploadResponse(BaseModel):
    upload_id: str
    size: int


class JobCreateRequest(BaseModel):
    upload_id: str = Field(min_length=1, max_length=128)
    paper_name: str = ""
    domain: DomainName = "statistics"
    eval_type: EvalType = "ref_free"
    generated_n: int = 8
    auto_refine: bool = True
    max_repair_rounds: int = 3
    console_output: ConsoleOutput = "quiet"
    skip_mineru: bool = False
    pdf_markdown_path: str = ""


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    run_dir: str
    status_path: str
    summary_path: str
    execution_status: str | None = None
    evaluation_status: str | None = None
    quality_status: str | None = None
    failure_code: str | None = None
    version: int | None = None


class CancelResponse(BaseModel):
    job_id: str
    canceled: bool
    message: str


class LogsResponse(BaseModel):
    job_id: str
    logs: list[str] = Field(default_factory=list)
    file: str | None = None
    content: str | None = None


class JobListItem(BaseModel):
    job_id: str
    paper_name: str | None = None
    status: str
    process_state: str = "none"
    cancelable: bool = False
    cancel_unavailable_reason: str = ""
    process_active: bool = False
    stage: str | None = None
    current_stage: str | None = None
    stage_attempt: int | None = None
    last_checkpoint_stage: str | None = None
    recovery_count: int = 0
    recovery_status: str = "none"
    recovery_error_code: str | None = None
    message: str | None = None
    updated_at: str | None = None
    repo_status: str | None = None
    eval_score: float | None = None
    run_dir: str
    execution_status: str | None = None
    evaluation_status: str | None = None
    quality_status: str | None = None
    failure_code: str | None = None
    version: int | None = None


class JobListResponse(BaseModel):
    jobs: list[JobListItem] = Field(default_factory=list)


class ArtifactSummaryResponse(BaseModel):
    job_id: str
    run_dir: str
    repo_dir: str
    results_dir: str
    logs_dir: str
    repo_file_count: int
    result_file_count: int
    log_file_count: int
    repo_ready: bool


class RepoFileEntry(BaseModel):
    path: str
    name: str
    type: Literal["file", "directory"]
    size: int | None = None
    modified_at: float


class RepoTreeResponse(BaseModel):
    job_id: str
    files: list[RepoFileEntry] = Field(default_factory=list)


class RepoFileResponse(BaseModel):
    job_id: str
    path: str
    size: int
    content: str


JsonDict = dict[str, Any]
