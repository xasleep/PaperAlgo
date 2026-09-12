from pathlib import Path
from decimal import Decimal, InvalidOperation
from threading import Lock

from fastapi import APIRouter, FastAPI, File, Header, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from codes.provider_registry import ProviderContractError, get_provider_registry

from .artifact_service import (
    artifact_summary,
    list_jobs,
    make_repo_zip,
    read_repo_file,
    repo_tree,
)
from . import job_service as job_service_module
from .config import (
    API_PREFIX,
    LOCAL_DEV_CORS_ORIGINS,
    TRUSTED_HOSTS,
    configured_database_path,
    configured_job_runtime,
)
from .database import normalize_database_path
from .errors import (
    FeatureNotSupportedError,
    InternalApiError,
    InvalidParameterError,
    JobCommandRejectedError,
    JobNotCancelableError,
    ProviderConfigurationError,
    SettingsNotConfiguredError,
    install_exception_handlers,
)
from .event_stream import (
    DEFAULT_REPLAY_LIMIT,
    MAX_REPLAY_LIMIT,
    format_gap_event,
    iter_job_event_stream,
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
from .job_repository import JobRepository
from .log_service import list_logs, read_log
from .path_security import validate_job_id
from .schemas import (
    ArtifactSummaryResponse,
    CancelResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobCommandRequest,
    JobCommandResponse,
    JobCommandsResponse,
    JobListResponse,
    JsonDict,
    LogsResponse,
    ProviderDiscovery,
    ProviderModelDiscovery,
    ProviderRegistryResponse,
    RepoFileResponse,
    RepoTreeResponse,
    SessionResponse,
    SettingsStatus,
    SettingsView,
    UploadResponse,
    WebSettings,
)
from .settings_store import (
    get_settings_status,
    get_settings_view,
    load_settings,
    save_settings,
)
from .static_ui import install_static_ui
from .storage_security import LocalStorageSecurityError
from .web_security import (
    LocalRequestSecurityMiddleware,
    LocalTrustedHostMiddleware,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    UploadBodyLimitMiddleware,
    issue_session,
    validate_request_origin,
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
    allow_headers=[
        "Accept",
        "Content-Type",
        "Idempotency-Key",
        "Last-Event-ID",
        "X-CSRF-Token",
    ],
)
app.add_middleware(UploadBodyLimitMiddleware)
app.add_middleware(LocalRequestSecurityMiddleware)
app.add_middleware(LocalTrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)
install_exception_handlers(app)

api = APIRouter(prefix=API_PREFIX)
MIN_GENERATED_N = 1
MAX_GENERATED_N = 32
_REPOSITORY_CACHE_LOCK = Lock()
_SQLITE_REPOSITORIES: dict[Path, JobRepository] = {}


def _sqlite_repository() -> JobRepository:
    database_path = normalize_database_path(configured_database_path())
    with _REPOSITORY_CACHE_LOCK:
        repository = _SQLITE_REPOSITORIES.get(database_path)
        if repository is None:
            repository = JobRepository(database_path)
            _SQLITE_REPOSITORIES[database_path] = repository
        return repository


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
    _validate_provider_settings(settings)
    try:
        save_settings(settings)
    except LocalStorageSecurityError as exc:
        raise InternalApiError("Failed to secure local settings storage.") from exc
    return get_settings_status()


@api.get("/settings", response_model=SettingsView)
def settings_view(response: Response) -> SettingsView:
    response.headers["Cache-Control"] = "no-store"
    return get_settings_view()


@api.get("/settings/status", response_model=SettingsStatus)
def settings_status() -> SettingsStatus:
    return get_settings_status()


@api.get("/providers", response_model=ProviderRegistryResponse)
def provider_discovery(response: Response) -> ProviderRegistryResponse:
    registry = get_provider_registry()
    providers: list[ProviderDiscovery] = []
    for provider_id in registry.provider_ids:
        models = [
            ProviderModelDiscovery(
                model_id=contract.model_id,
                max_n=contract.max_n,
                context_window=contract.context_window,
                max_output_tokens=contract.max_output_tokens,
                json_schema_support=contract.json_schema_support,
                usage_support=contract.usage_support,
                cache_token_support=contract.cache_token_support,
            )
            for model_id in registry.model_ids(provider_id)
            for contract in (registry.get(provider_id, model_id),)
        ]
        if models:
            providers.append(ProviderDiscovery(provider_id=provider_id, models=models))
    response.headers["Cache-Control"] = "no-store"
    return ProviderRegistryResponse(
        registry_version=registry.version,
        providers=providers,
    )


def _raise_provider_configuration_error(exc: ProviderContractError) -> None:
    raise ProviderConfigurationError(
        exc.code,
        str(exc),
        details=exc.safe_details,
    ) from exc


def _validate_provider_settings(settings: WebSettings) -> None:
    try:
        job_service_module.validate_provider_settings_contract(
            settings,
            registry=get_provider_registry(),
        )
    except ProviderContractError as exc:
        _raise_provider_configuration_error(exc)


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
    _validate_cost_budget_payload(payload)


def _validate_cost_budget_payload(payload: JobCreateRequest) -> None:
    if payload.cost_budget_policy == "none":
        if payload.cost_budget_currency is not None or payload.cost_budget_amount is not None:
            raise InvalidParameterError(
                "cost_budget_currency and cost_budget_amount require a hard budget policy.",
                details={"parameter": "cost_budget_policy"},
            )
        return
    currency = payload.cost_budget_currency
    amount = payload.cost_budget_amount
    if (
        not isinstance(currency, str)
        or not currency
        or len(currency) > 16
        or not currency.isascii()
        or not currency.isprintable()
    ):
        raise InvalidParameterError(
            "cost_budget_currency must be a printable currency code.",
            details={"parameter": "cost_budget_currency"},
        )
    if not isinstance(amount, str) or not amount:
        raise InvalidParameterError(
            "cost_budget_amount must be a decimal string.",
            details={"parameter": "cost_budget_amount"},
        )
    try:
        decimal_amount = Decimal(amount)
    except (InvalidOperation, ValueError) as exc:
        raise InvalidParameterError(
            "cost_budget_amount must be a decimal string.",
            details={"parameter": "cost_budget_amount"},
        ) from exc
    if not decimal_amount.is_finite() or decimal_amount < 0:
        raise InvalidParameterError(
            "cost_budget_amount must be finite and non-negative.",
            details={"parameter": "cost_budget_amount"},
        )


def _job_request_dict(payload: JobCreateRequest) -> dict[str, object]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    return payload.dict()


def _sqlite_job_view(
    job: dict[str, object],
    repository: JobRepository | None = None,
) -> JsonDict:
    repository = repository or _sqlite_repository()
    job_id = str(job["job_id"])
    execution_status = str(job["execution_status"])
    process = repository.get_process(job_id)
    cancel_command = repository.get_cancel_command(job_id)
    run_dir = job_service_module.RUNS_DIR / job_id
    terminal = execution_status in {"completed", "failed", "canceled"}
    cancel_requested = cancel_command is not None and cancel_command["status"] in {
        "pending",
        "claimed",
    }
    identity_unresolved = bool(
        execution_status == "running"
        and process is not None
        and process.get("launch_state") == "identity_unresolved"
    )
    process_active = bool(
        execution_status == "running"
        and process is not None
        and process.get("launch_state") == "registered"
        and process.get("pid")
        and process.get("exited_at") is None
    )
    if process_active:
        process_state = "active"
    elif identity_unresolved:
        process_state = "detached"
    elif terminal:
        process_state = "finished"
    else:
        process_state = "none"
    recovery_status = str(job.get("recovery_status") or "none")
    recovery_active = recovery_status in {"prepared", "running"}
    return {
        **job,
        "status": execution_status,
        "process_state": process_state,
        "cancelable": not terminal and not identity_unresolved,
        "cancel_unavailable_reason": (
            "process_identity_unresolved"
            if identity_unresolved
            else ("already_finished" if terminal else "")
        ),
        "process_active": process_active,
        "stage": job.get("current_stage") or execution_status,
        "current_stage": job.get("current_stage"),
        "stage_attempt": job.get("current_stage_attempt"),
        "last_checkpoint_stage": job.get("last_checkpoint_stage"),
        "recovery_count": int(job.get("recovery_count") or 0),
        "recovery_status": recovery_status,
        "recovery_error_code": job.get("recovery_error_code"),
        "message": (
            "Pipeline launch identity is unresolved; new work is blocked for safety."
            if identity_unresolved
            else (
                "Pipeline recovery is starting from the last verified stage boundary."
                if recovery_active
                else (
                "Cancellation requested."
                if cancel_requested
                else (
                    "Queued for the SQLite worker runtime."
                    if execution_status == "queued"
                    else None
                )
                )
            )
        ),
        "repo_status": None,
        "eval_score": None,
        "run_dir": str(run_dir),
        "cost_summary": repository.cost_summary(job_id),
    }


def _sqlite_create_response(
    job: dict[str, object],
    repository: JobRepository | None = None,
) -> JobCreateResponse:
    view = _sqlite_job_view(job, repository)
    run_dir = job_service_module.RUNS_DIR / str(job["job_id"])
    return JobCreateResponse(
        job_id=str(job["job_id"]),
        status=str(view["status"]),
        run_dir=str(run_dir),
        status_path=str(run_dir / "run_status.json"),
        summary_path=str(run_dir / "run_summary.json"),
        execution_status=str(job["execution_status"]),
        evaluation_status=str(job["evaluation_status"]),
        quality_status=str(job["quality_status"]),
        version=int(job["version"]),
        reproduce_provider=job.get("reproduce_provider"),
        reproduce_model=job.get("reproduce_model"),
        evaluation_provider=job.get("evaluation_provider"),
        evaluation_model=job.get("evaluation_model"),
        evaluation_fallback_models=job.get("evaluation_fallback_models"),
        provider_registry_version=job.get("provider_registry_version"),
        provider_contract_fingerprint=job.get("provider_contract_fingerprint"),
        cost_budget_policy=job.get("cost_budget_policy"),
        cost_budget_currency=job.get("cost_budget_currency"),
        cost_budget_amount=job.get("cost_budget_amount"),
        cost_summary=view.get("cost_summary"),
    )


def _command_response(command: dict[str, object]) -> JobCommandResponse:
    return JobCommandResponse(
        command_id=int(command["command_id"]),
        job_id=str(command["job_id"]),
        command_type=str(command["command_type"]),  # type: ignore[arg-type]
        status=str(command["status"]),  # type: ignore[arg-type]
        request_status=str(command["request_status"]),  # type: ignore[arg-type]
        error_code=(
            None if command.get("error_code") is None else str(command["error_code"])
        ),
        rejection_code=(
            None
            if command.get("rejection_code") is None
            else str(command["rejection_code"])
        ),
        result_code=(
            None if command.get("result_code") is None else str(command["result_code"])
        ),
        created_at=str(command["created_at"]),
        claimed_at=(
            None if command.get("claimed_at") is None else str(command["claimed_at"])
        ),
        completed_at=(
            None
            if command.get("completed_at") is None
            else str(command["completed_at"])
        ),
        updated_at=(
            None if command.get("updated_at") is None else str(command["updated_at"])
        ),
        version=int(command["version"]),
    )


def _raise_if_command_rejected(command: dict[str, object]) -> None:
    if command.get("request_status") != "rejected":
        return
    reason = str(
        command.get("rejection_code")
        or command.get("error_code")
        or "invalid_state"
    )
    raise JobCommandRejectedError(
        command_id=int(command["command_id"]),
        command_type=str(command["command_type"]),
        reason=reason,
    )


def _parse_last_event_id(value: str | None) -> int:
    if value is None or value == "":
        return 0
    try:
        parsed = int(value)
    except ValueError as exc:
        raise InvalidParameterError(
            "Last-Event-ID must be a non-negative integer.",
            details={"header": "Last-Event-ID"},
        ) from exc
    if parsed < 0:
        raise InvalidParameterError(
            "Last-Event-ID must be a non-negative integer.",
            details={"header": "Last-Event-ID"},
        )
    return parsed


SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@api.post(
    "/jobs",
    response_model=JobCreateResponse,
    response_model_exclude_none=True,
)
def create_job(
    payload: JobCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JobCreateResponse:
    _validate_job_parameters(payload)
    runtime = configured_job_runtime()
    request_data = _job_request_dict(payload)
    repository = _sqlite_repository() if runtime == "sqlite" else None
    if repository is not None:
        replay = repository.find_idempotent_job(
            request=request_data,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return _sqlite_create_response(replay, repository)

    settings = load_settings()
    if settings is None:
        raise SettingsNotConfiguredError()
    _validate_provider_settings(settings)

    pdf_path = resolve_upload(payload.upload_id)
    resolved_paper_name = sanitize_name(payload.paper_name, "paper")
    job_id = make_job_id(resolved_paper_name)
    if repository is not None:
        provider_snapshot = job_service_module.build_provider_selection_snapshot(
            settings
        )
        job, _ = repository.create_job(
            job_id=job_id,
            request=request_data,
            paper_name=resolved_paper_name,
            idempotency_key=idempotency_key,
            provider_snapshot=provider_snapshot,
        )
        return _sqlite_create_response(job, repository)

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


@api.get("/jobs/{job_id}/events")
def job_events(
    request: Request,
    job_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    last_event_id_query: int | None = Query(default=None, ge=0, alias="last_event_id"),
    replay_limit: int = Query(default=DEFAULT_REPLAY_LIMIT, ge=1, le=MAX_REPLAY_LIMIT),
    follow: bool = Query(default=True),
    eventsource: bool = Query(default=False),
):
    origin_error = validate_request_origin(request)
    if origin_error is not None:
        return origin_error
    if configured_job_runtime() != "sqlite":
        raise FeatureNotSupportedError(
            "Job event streaming is available only for the SQLite runtime."
        )
    validated_job_id = validate_job_id(job_id)
    parsed_last_event_id = (
        last_event_id_query
        if last_event_id_query is not None and (last_event_id is None or last_event_id == "")
        else _parse_last_event_id(last_event_id)
    )
    repository = _sqlite_repository()
    replay = repository.list_job_events_after(
        validated_job_id,
        parsed_last_event_id,
        limit=replay_limit,
    )
    if replay.gap_detected:
        return StreamingResponse(
            iter(
                [
                    format_gap_event(
                        job_id=validated_job_id,
                        last_event_id=parsed_last_event_id,
                        replay_limit=replay_limit,
                        available_event_count=replay.available_event_count,
                        latest_event_id=replay.latest_event_id,
                    )
                ]
            ),
            status_code=200 if eventsource else 409,
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    return StreamingResponse(
        iter_job_event_stream(
            request,
            repository=repository,
            job_id=validated_job_id,
            last_event_id=parsed_last_event_id,
            replay_limit=replay_limit,
            follow=follow,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@api.post(
    "/jobs/{job_id}/commands",
    response_model=JobCommandResponse,
    response_model_exclude_none=True,
)
def create_job_command(
    job_id: str,
    payload: JobCommandRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JobCommandResponse:
    if configured_job_runtime() != "sqlite":
        raise FeatureNotSupportedError(
            "Persisted job commands are available only for the SQLite runtime."
        )
    repository = _sqlite_repository()
    command = repository.request_job_command(
        validate_job_id(job_id),
        command_type=payload.command_type,
        idempotency_key=idempotency_key,
    )
    _raise_if_command_rejected(command)
    return _command_response(command)


@api.get(
    "/jobs/{job_id}/commands",
    response_model=JobCommandsResponse,
    response_model_exclude_none=True,
)
def job_commands(
    job_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> JobCommandsResponse:
    if configured_job_runtime() != "sqlite":
        raise FeatureNotSupportedError(
            "Persisted job commands are available only for the SQLite runtime."
        )
    repository = _sqlite_repository()
    return JobCommandsResponse(
        commands=[
            _command_response(command)
            for command in repository.list_job_commands(
                validate_job_id(job_id),
                limit=limit,
            )
        ]
    )


@api.get(
    "/jobs/{job_id}/commands/{command_id}",
    response_model=JobCommandResponse,
    response_model_exclude_none=True,
)
def job_command(job_id: str, command_id: int) -> JobCommandResponse:
    if configured_job_runtime() != "sqlite":
        raise FeatureNotSupportedError(
            "Persisted job commands are available only for the SQLite runtime."
        )
    repository = _sqlite_repository()
    return _command_response(
        repository.get_job_command(validate_job_id(job_id), command_id)
    )


@api.get(
    "/jobs",
    response_model=JobListResponse,
    response_model_exclude_none=True,
)
def jobs(limit: int = Query(default=50, ge=1, le=200)) -> JobListResponse:
    if configured_job_runtime() == "sqlite":
        repository = _sqlite_repository()
        return JobListResponse(
            jobs=[
                _sqlite_job_view(job, repository)
                for job in repository.list_jobs(limit=limit)
            ]
        )
    return JobListResponse(jobs=list_jobs(limit=limit))


@api.get("/jobs/{job_id}", response_model=None)
def job_status(job_id: str) -> JsonDict:
    if configured_job_runtime() == "sqlite":
        repository = _sqlite_repository()
        return _sqlite_job_view(
            repository.get_job(validate_job_id(job_id)),
            repository,
        )
    return get_job_status(job_id)


@api.post("/jobs/{job_id}/cancel", response_model=CancelResponse)
def cancel(job_id: str) -> CancelResponse:
    if configured_job_runtime() == "sqlite":
        repository = _sqlite_repository()
        validated_job_id = validate_job_id(job_id)
        command = repository.request_cancel(validated_job_id)
        job = repository.get_job(validated_job_id)
        canceled = str(job["execution_status"]) == "canceled"
        return CancelResponse(
            job_id=validated_job_id,
            canceled=canceled,
            message=(
                "Cancellation completed."
                if canceled or command["status"] == "completed"
                else "Cancellation requested."
            ),
        )
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
