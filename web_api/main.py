from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .artifact_service import (
    artifact_summary,
    list_jobs,
    make_repo_zip,
    read_repo_file,
    repo_tree,
)
from .config import LOCAL_DEV_CORS_ORIGINS
from .errors import (
    InternalApiError,
    InvalidParameterError,
    SettingsNotConfiguredError,
    UnsupportedFileTypeError,
    install_exception_handlers,
)
from .job_service import (
    cancel_job,
    get_job_status,
    get_job_summary,
    make_job_id,
    sanitize_name,
    save_upload,
    start_job,
)
from .log_service import list_logs, read_log
from .schemas import (
    CancelResponse,
    ConsoleOutput,
    DomainName,
    EvalType,
    ArtifactSummaryResponse,
    JobCreateResponse,
    JobListResponse,
    JsonDict,
    LogsResponse,
    RepoFileResponse,
    RepoTreeResponse,
    SettingsStatus,
    WebSettings,
)
from .settings_store import get_settings_status, load_settings, save_settings
from .static_ui import install_static_ui, spa_index_response
from .storage_security import LocalStorageSecurityError


app = FastAPI(title="Paper2Code Agent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_DEV_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Accept", "Content-Type"],
)
install_exception_handlers(app)
MIN_GENERATED_N = 1
MAX_GENERATED_N = 32
MAX_REPAIR_ROUNDS_LIMIT = 10


@app.get("/health")
def health() -> JsonDict:
    return {"status": "ok"}


@app.post("/settings", response_model=SettingsStatus)
def update_settings(settings: WebSettings) -> SettingsStatus:
    try:
        save_settings(settings)
    except LocalStorageSecurityError as exc:
        raise InternalApiError("Failed to secure local settings storage.") from exc
    return get_settings_status()


@app.get("/settings/status", response_model=SettingsStatus)
def settings_status() -> SettingsStatus:
    return get_settings_status()


@app.get("/jobs", response_model=JobListResponse)
def jobs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> JobListResponse | FileResponse:
    ui_response = spa_index_response(request)
    if ui_response is not None:
        return ui_response
    return JobListResponse(jobs=list_jobs(limit=limit))


@app.post("/jobs", response_model=JobCreateResponse)
async def create_job(
    file: UploadFile = File(...),
    paper_name: str = Form(""),
    domain: DomainName = Form("statistics"),
    eval_type: EvalType = Form("ref_free"),
    generated_n: int = Form(8),
    auto_refine: bool = Form(True),
    max_repair_rounds: int = Form(3),
    console_output: ConsoleOutput = Form("quiet"),
    skip_mineru: bool = Form(False),
    pdf_markdown_path: str = Form(""),
) -> JobCreateResponse:
    settings = load_settings()
    if settings is None:
        raise SettingsNotConfiguredError()

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise UnsupportedFileTypeError("Only PDF uploads are supported.")

    if generated_n < MIN_GENERATED_N or generated_n > MAX_GENERATED_N:
        raise InvalidParameterError(
            f"generated_n must be between {MIN_GENERATED_N} and {MAX_GENERATED_N}.",
            details={
                "parameter": "generated_n",
                "min": MIN_GENERATED_N,
                "max": MAX_GENERATED_N,
            },
        )

    if max_repair_rounds < 0 or max_repair_rounds > MAX_REPAIR_ROUNDS_LIMIT:
        raise InvalidParameterError(
            f"max_repair_rounds must be between 0 and {MAX_REPAIR_ROUNDS_LIMIT}.",
            details={
                "parameter": "max_repair_rounds",
                "min": 0,
                "max": MAX_REPAIR_ROUNDS_LIMIT,
            },
        )

    resolved_paper_name = sanitize_name(paper_name or file.filename, "paper")
    job_id = make_job_id(resolved_paper_name)
    upload_path = save_upload(job_id, file.filename, file.file)

    if skip_mineru and not pdf_markdown_path:
        raise InvalidParameterError(
            "pdf_markdown_path is required when skip_mineru is true.",
            details={"parameter": "pdf_markdown_path"},
        )

    job = start_job(
        job_id=job_id,
        pdf_path=upload_path,
        paper_name=resolved_paper_name,
        settings=settings,
        domain=domain,
        eval_type=eval_type,
        generated_n=generated_n,
        auto_refine=auto_refine,
        max_repair_rounds=max_repair_rounds,
        console_output=console_output,
        skip_mineru=skip_mineru,
        pdf_markdown_path=pdf_markdown_path,
    )
    return JobCreateResponse(**job)


@app.get("/jobs/{job_id}/artifacts", response_model=ArtifactSummaryResponse)
def job_artifacts(job_id: str) -> ArtifactSummaryResponse:
    return ArtifactSummaryResponse(**artifact_summary(job_id))


@app.get("/jobs/{job_id}/repo/tree", response_model=RepoTreeResponse)
def job_repo_tree(
    job_id: str,
    max_files: int = Query(default=500, ge=1, le=2000),
) -> RepoTreeResponse:
    return RepoTreeResponse(job_id=job_id, files=repo_tree(job_id, max_files=max_files))


@app.get("/jobs/{job_id}/repo/file", response_model=RepoFileResponse)
def job_repo_file(job_id: str, path: str = Query(..., min_length=1)) -> RepoFileResponse:
    return RepoFileResponse(**read_repo_file(job_id, path))


@app.get("/jobs/{job_id}/repo/download")
def job_repo_download(job_id: str) -> FileResponse:
    zip_path = make_repo_zip(job_id)
    return FileResponse(
        path=str(zip_path),
        filename=f"{job_id}_repo.zip",
        media_type="application/zip",
    )


@app.get("/jobs/{job_id}", response_model=None)
def job_status(request: Request, job_id: str) -> JsonDict | FileResponse:
    ui_response = spa_index_response(request)
    if ui_response is not None:
        return ui_response
    return get_job_status(job_id)


@app.get("/jobs/{job_id}/summary")
def job_summary(job_id: str) -> JsonDict:
    return get_job_summary(job_id)


@app.get("/jobs/{job_id}/logs", response_model=LogsResponse)
def job_logs(
    job_id: str,
    file: str | None = Query(default=None),
    tail_lines: int = Query(default=200, ge=1, le=2000),
) -> LogsResponse:
    logs = list_logs(job_id)
    if file is None:
        return LogsResponse(job_id=job_id, logs=logs)
    content = read_log(job_id, file, tail_lines=tail_lines)
    return LogsResponse(job_id=job_id, logs=logs, file=file, content=content)


@app.post("/jobs/{job_id}/cancel", response_model=CancelResponse)
def cancel(job_id: str) -> CancelResponse:
    canceled, message = cancel_job(job_id)
    return CancelResponse(job_id=job_id, canceled=True, message=message)


install_static_ui(app)
