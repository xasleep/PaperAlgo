from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def error_payload(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        }
    }


class ContractError(Exception):
    status_code = 500
    code = "internal_error"
    default_message = "Internal server error."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.details = details or {}

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=error_payload(self.code, self.message, self.details),
        )


class SettingsNotConfiguredError(ContractError, ValueError):
    status_code = 400
    code = "settings_not_configured"
    default_message = "API settings are not configured."


class InvalidJobIdError(ContractError, ValueError):
    status_code = 400
    code = "invalid_job_id"
    default_message = "Job ID is invalid."


class InvalidUploadError(ContractError, ValueError):
    status_code = 400
    code = "invalid_upload"
    default_message = "Upload content is invalid."


class InvalidParameterError(ContractError, ValueError):
    status_code = 400
    code = "invalid_parameter"
    default_message = "Request parameters are invalid."


class InvalidRepoPathError(ContractError, ValueError):
    status_code = 400
    code = "invalid_repo_path"
    default_message = "Repository path is invalid."


class JobNotFoundError(ContractError, FileNotFoundError):
    status_code = 404
    code = "job_not_found"
    default_message = "Job not found."


class RepoNotAvailableError(ContractError, FileNotFoundError):
    status_code = 404
    code = "repo_not_available"
    default_message = "Repository is not available."


class RepoFileNotFoundError(ContractError, FileNotFoundError):
    status_code = 404
    code = "artifact_not_available"
    default_message = "Repository file not found."


class LogNotFoundError(ContractError, FileNotFoundError):
    status_code = 404
    code = "log_not_found"
    default_message = "Log file not found."


class FileTooLargeError(ContractError, ValueError):
    status_code = 413
    code = "file_too_large"
    default_message = "File is too large to preview."


class BinaryFileNotSupportedError(ContractError, ValueError):
    status_code = 415
    code = "binary_file_not_supported"
    default_message = "Binary files are not supported for preview."


class UnsupportedFileTypeError(ContractError, ValueError):
    status_code = 415
    code = "unsupported_file_type"
    default_message = "File type is not supported."


class FeatureNotSupportedError(ContractError, ValueError):
    status_code = 422
    code = "feature_not_supported"
    default_message = "The requested feature is not supported by the local Web API."


class ProviderConfigurationError(ContractError, ValueError):
    status_code = 422
    code = "provider_configuration_invalid"
    default_message = "Provider/model configuration is invalid."

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.code = code


class IdempotencyConflictError(ContractError, ValueError):
    status_code = 409
    code = "idempotency_conflict"
    default_message = "Idempotency-Key was already used with a different request."


class InvalidStateTransitionError(ContractError, ValueError):
    status_code = 409
    code = "invalid_state_transition"
    default_message = "The requested job state transition is not allowed."


class OptimisticLockConflictError(ContractError):
    status_code = 409
    code = "optimistic_lock_conflict"
    default_message = "The job was updated by another control-plane operation."


class TextEncodingNotSupportedError(ContractError, ValueError):
    status_code = 415
    code = "unsupported_file_type"
    default_message = "Only UTF-8 text preview is supported."


class JobAlreadyFinishedError(ContractError):
    status_code = 409
    code = "job_already_finished"
    default_message = "Job is already finished."


class JobNotCancelableError(ContractError):
    status_code = 409
    code = "job_not_cancelable"
    default_message = "Job cannot be canceled."

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(
            message or self.default_message,
            details={"reason": reason},
        )
        self.reason = reason


class JobCommandRejectedError(ContractError):
    status_code = 409
    code = "job_command_rejected"
    default_message = "Job command was rejected."

    def __init__(
        self,
        *,
        command_id: int,
        command_type: str,
        reason: str,
        message: str | None = None,
    ) -> None:
        super().__init__(
            message or self.default_message,
            details={
                "command_id": command_id,
                "command_type": command_type,
                "reason": reason,
            },
        )
        self.command_id = command_id
        self.command_type = command_type
        self.reason = reason


class ArtifactNotAvailableError(ContractError, FileNotFoundError):
    status_code = 404
    code = "artifact_not_available"
    default_message = "Artifact is not available."


class InternalApiError(ContractError):
    status_code = 500
    code = "internal_error"
    default_message = "Internal server error."


def safe_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Return useful validation locations without reflecting request inputs."""

    safe_errors: list[dict[str, Any]] = []
    for error in exc.errors():
        location = [
            item if isinstance(item, (str, int)) and not isinstance(item, bool) else "?"
            for item in error.get("loc", ())
        ]
        safe_errors.append(
            {
                "type": str(error.get("type") or "validation_error"),
                "loc": location,
                "msg": str(error.get("msg") or "Invalid value."),
            }
        )
    return safe_errors


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ContractError)
    async def handle_contract_error(
        request: Request,
        exc: ContractError,
    ) -> JSONResponse:
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_payload(
                "validation_error",
                "Request validation failed.",
                {"errors": safe_validation_errors(exc)},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        message = "API endpoint not found." if exc.status_code == 404 else "HTTP request failed."
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(code, message),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        return InternalApiError().to_response()
