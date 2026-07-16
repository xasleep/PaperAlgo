from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


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


class ArtifactNotAvailableError(ContractError, FileNotFoundError):
    status_code = 404
    code = "artifact_not_available"
    default_message = "Artifact is not available."


class InternalApiError(ContractError):
    status_code = 500
    code = "internal_error"
    default_message = "Internal server error."


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ContractError)
    async def handle_contract_error(
        request: Request,
        exc: ContractError,
    ) -> JSONResponse:
        return exc.to_response()

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        return InternalApiError().to_response()
