from fastapi import APIRouter, FastAPI, File, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .artifact_service import (
    artifact_summary,
    list_jobs,
    make_repo_zip,
    read_repo_file,
    repo_tree,
)
from .config import API_PREFIX, LOCAL_DEV_CORS_ORIGINS, TRUSTED_HOSTS
from .errors import (
    FeatureNotSupportedError,
    InternalApiError,
    InvalidParameterError,
    SettingsNotConfiguredError,
    install_exception_handlers,
)
from .job_service import (
    cancel_job,
    get_job_status,
    get_job_summary,
    make_job_id,
    make_upload_id,
    resolve_upload,
    sanitize_name,
    save_upload,
    start_job,
)
from .log_service import list_logs, read_log
from .schemas import (
    ArtifactSummaryResponse,
    CancelResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobListResponse,
    JsonDict,
    LogsResponse,
    RepoFileResponse,
    RepoTreeResponse,
    SessionResponse,
    SettingsStatus,
    UploadResponse,
    WebSettings,
)
from .settings_store import get_settings_status, load_settings, save_settings
from .static_ui import install_static_ui
from .storage_security import LocalStorageSecurityError
from .web_security import (
    LocalRequestSecurityMiddleware,
    LocalTrustedHostMiddleware,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    UploadBodyLimitMiddleware,
    issue_session,
)


app = FastAPI(
    title="Paper2Code Agent API",
    version="0.2.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=f"{API_PREFIX}/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_DEV_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Content-Type", "X-CSRF-Token"],
)
app.add_middleware(UploadBodyLimitMiddleware)
app.add_middleware(LocalRequestSecurityMiddleware)
app.add_middleware(LocalTrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)
install_exception_handlers(app)

api = APIRouter(prefix=API_PREFIX)
MIN_GENERATED_N = 1
MAX_GENERATED_N = 32
MAX_REPAIR_ROUNDS_LIMIT = 10


@api.get("/health")
def health() -> JsonDict:
    return {"status": "ok"}


@api.get("/session", response_model=SessionResponse)
def local_session(request: Request) -> SessionResponse:
    session_id, csrf_token = issue_session(request)
    response = SessionResponse(csrf_token=csrf_token)
    request.state.session_cookie = session_id
    return response


@app.middleware("http")
async def set_session_cookie(request: Request, call_next):
    response = await call_next(request)
    session_id = getattr(request.state, "session_cookie", None)
    if session_id:
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_id,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
    return response


@api.post("/settings", response_model=SettingsStatus)
def update_settings(settings: WebSettings) -> SettingsStatus:
    try:
        save_settings(settings)
    except LocalStorageSecurityError as exc:
        raise InternalApiError("Failed to secure local settings storage.") from exc
    return get_settings_status()


@api.get("/settings/status", response_model=SettingsStatus)
def settings_status() -> SettingsStatus:
    return get_settings_status()


@api.post("/uploads", response_model=UploadResponse)
async def upload_pdf(file: UploadFile = File(...)) -> UploadResponse:
    upload_id = make_upload_id()
    try:
        path = await run_in_threadpool(
            save_upload,
            upload_id,
            file.filename or "",
            file.file,
        )
    finally:
        await file.close()
    return UploadResponse(upload_id=upload_id, size=path.stat().st_size)


def _validate_job_parameters(payload: JobCreateRequest) -> None:
    if payload.eval_type == "ref_based":
        raise FeatureNotSupportedError(
            "ref_based evaluation is not supported by the local Web API.",
            details={"parameter": "eval_type", "value": "ref_based"},
        )
    if payload.generated_n < MIN_GENERATED_N or payload.generated_n > MAX_GENERATED_N:
        raise InvalidParameterError(
            f"generated_n must be between {MIN_GENERATED_N} and {MAX_GENERATED_N}.",
            details={
                "parameter": "generated_n",
                "min": MIN_GENERATED_N,
                "max": MAX_GENERATED_N,
            },
        )
    if (
        payload.max_repair_rounds < 0
        or payload.max_repair_rounds > MAX_REPAIR_ROUNDS_LIMIT
    ):
        raise InvalidParameterError(
            f"max_repair_rounds must be between 0 and {MAX_REPAIR_ROUNDS_LIMIT}.",
            details={
                "parameter": "max_repair_rounds",
                "min": 0,
                "max": MAX_REPAIR_ROUNDS_LIMIT,
            },
        )
    if payload.skip_mineru and not payload.pdf_markdown_path:
        raise InvalidParameterError(
            "pdf_markdown_path is required when skip_mineru is true.",
            details={"parameter": "pdf_markdown_path"},
        )


@api.post("/jobs", response_model=JobCreateResponse)
def create_job(payload: JobCreateRequest) -> JobCreateResponse:
    _validate_job_parameters(payload)
    settings = load_settings()
    if settings is None:
        raise SettingsNotConfiguredError()

    pdf_path = resolve_upload(payload.upload_id)
    resolved_paper_name = sanitize_name(payload.paper_name, "paper")
    job_id = make_job_id(resolved_paper_name)
    job = start_job(
        job_id=job_id,
        pdf_path=pdf_path,
        paper_name=resolved_paper_name,
        settings=settings,
        domain=payload.domain,
        eval_type=payload.eval_type,
        generated_n=payload.generated_n,
        auto_refine=payload.auto_refine,
        max_repair_rounds=payload.max_repair_rounds,
        console_output=payload.console_output,
        skip_mineru=payload.skip_mineru,
        pdf_markdown_path=payload.pdf_markdown_path,
    )
    return JobCreateResponse(**job)


@api.get("/jobs", response_model=JobListResponse)
def jobs(limit: int = Query(default=50, ge=1, le=200)) -> JobListResponse:
    return JobListResponse(jobs=list_jobs(limit=limit))


@api.get("/jobs/{job_id}", response_model=None)
def job_status(job_id: str) -> JsonDict:
    return get_job_status(job_id)


@api.post("/jobs/{job_id}/cancel", response_model=CancelResponse)
def cancel(job_id: str) -> CancelResponse:
    canceled, message = cancel_job(job_id)
    return CancelResponse(job_id=job_id, canceled=canceled, message=message)


@api.get("/jobs/{job_id}/artifacts", response_model=ArtifactSummaryResponse)
def job_artifacts(job_id: str) -> ArtifactSummaryResponse:
    return ArtifactSummaryResponse(**artifact_summary(job_id))


@api.get("/jobs/{job_id}/export")
def job_export(job_id: str) -> FileResponse:
    zip_path = make_repo_zip(job_id)
    return FileResponse(
        path=str(zip_path),
        filename=f"{job_id}_repo.zip",
        media_type="application/zip",
    )


@api.get("/jobs/{job_id}/summary")
def job_summary(job_id: str) -> JsonDict:
    return get_job_summary(job_id)


@api.get("/jobs/{job_id}/logs", response_model=LogsResponse)
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


@api.get("/jobs/{job_id}/repo/tree", response_model=RepoTreeResponse)
def job_repo_tree(
    job_id: str,
    max_files: int = Query(default=500, ge=1, le=2000),
) -> RepoTreeResponse:
    return RepoTreeResponse(job_id=job_id, files=repo_tree(job_id, max_files=max_files))


@api.get("/jobs/{job_id}/repo/file", response_model=RepoFileResponse)
def job_repo_file(job_id: str, path: str = Query(..., min_length=1)) -> RepoFileResponse:
    return RepoFileResponse(**read_repo_file(job_id, path))


app.include_router(api)
install_static_ui(app)
