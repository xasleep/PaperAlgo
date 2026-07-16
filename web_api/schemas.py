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
    provider: str
    model: str
    base_url: str = ""
    has_api_key: bool


class EvaluationSettingsStatus(ProviderSettingsStatus):
    fallback_models: list[str] = Field(default_factory=list)


class SettingsStatus(BaseModel):
    configured: bool
    reproduce: ProviderSettingsStatus | None = None
    evaluation: EvaluationSettingsStatus | None = None


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
    message: str | None = None
    updated_at: str | None = None
    repo_status: str | None = None
    eval_score: float | None = None
    run_dir: str


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
