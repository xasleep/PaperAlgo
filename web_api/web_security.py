from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Sequence
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.formparsers import MultiPartException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import API_PREFIX, LOCAL_DEV_CORS_ORIGINS, MAX_UPLOAD_REQUEST_BYTES
from .errors import error_payload


MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SESSION_COOKIE_NAME = "paper2code_session"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
_CSRF_SECRET = secrets.token_bytes(32)
_SESSION_ID_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _api_error(status_code: int, code: str, message: str) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content=error_payload(code, message),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


class UploadBodyTooLarge(MultiPartException):
    """Abort multipart parsing while preserving Starlette's spool cleanup path."""


class UploadBodyLimitMiddleware:
    """Bound the raw upload request before Starlette parses multipart files."""

    def __init__(self, app: ASGIApp, max_body_bytes: int | None = None) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    def _request_limit(self) -> int:
        limit = MAX_UPLOAD_REQUEST_BYTES if self.max_body_bytes is None else self.max_body_bytes
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("Upload request limit must be a positive integer.")
        return limit

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                parsed = int(value.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                return None
            return parsed if parsed >= 0 else None
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() != "POST"
            or scope.get("path") != f"{API_PREFIX}/uploads"
        ):
            await self.app(scope, receive, send)
            return

        limit = self._request_limit()
        too_large_response = _api_error(
            413,
            "file_too_large",
            f"Upload request exceeds the configured limit of {limit} bytes.",
        )
        content_length = self._content_length(scope)
        if content_length is not None and content_length > limit:
            await too_large_response(scope, receive, send)
            return

        received_bytes = 0
        limit_exceeded = False
        response_started = False
        replacement_sent = False

        async def limited_receive():
            nonlocal received_bytes, limit_exceeded
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > limit:
                    limit_exceeded = True
                    raise UploadBodyTooLarge(
                        f"Upload request exceeds the configured limit of {limit} bytes."
                    )
            return message

        async def limited_send(message) -> None:
            nonlocal response_started, replacement_sent
            if limit_exceeded:
                if response_started:
                    await send(message)
                    return
                if not replacement_sent:
                    replacement_sent = True
                    await too_large_response(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except UploadBodyTooLarge:
            if response_started:
                raise
            if not replacement_sent:
                await too_large_response(scope, receive, send)


def _host_name(host_header: str) -> str:
    value = host_header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end >= 0 else value
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            return host
    return value


class LocalTrustedHostMiddleware(TrustedHostMiddleware):
    """TrustedHostMiddleware with bracketed IPv6 and JSON API failures."""

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Sequence[str],
    ) -> None:
        super().__init__(app, allowed_hosts=allowed_hosts, www_redirect=False)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        host = _host_name(headers.get(b"host", b"").decode("latin-1"))
        if host in self.allowed_hosts:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if path == API_PREFIX or path.startswith(f"{API_PREFIX}/"):
            response: Response = _api_error(
                400,
                "invalid_host",
                "Host header is not allowed for this local service.",
            )
        else:
            response = Response("Invalid host header", status_code=400)
        await response(scope, receive, send)


def _request_origin(request: Request) -> str:
    return f"{request.url.scheme.lower()}://{request.headers.get('host', '').lower()}".rstrip(
        "/"
    )


def _referer_origin(referer: str) -> str | None:
    try:
        parsed = urlsplit(referer)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def allowed_request_origins(request: Request) -> set[str]:
    return {_request_origin(request), *LOCAL_DEV_CORS_ORIGINS}


def validate_request_origin(request: Request) -> JSONResponse | None:
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return _api_error(403, "cross_site_request", "Cross-site requests are not allowed.")

    allowed = allowed_request_origins(request)
    origin = request.headers.get("origin")
    if origin is not None:
        if origin.rstrip("/") not in allowed:
            return _api_error(403, "origin_not_allowed", "Request origin is not allowed.")
        return None

    referer = request.headers.get("referer")
    if referer is None or _referer_origin(referer) not in allowed:
        return _api_error(
            403,
            "origin_not_allowed",
            "A same-origin Origin or Referer header is required.",
        )
    return None


def _csrf_token(session_id: str) -> str:
    return hmac.new(_CSRF_SECRET, session_id.encode("ascii"), hashlib.sha256).hexdigest()


def _valid_session_id(session_id: str) -> bool:
    return (
        20 <= len(session_id) <= 128
        and all(char in _SESSION_ID_CHARS for char in session_id)
    )


def issue_session(request: Request) -> tuple[str, str]:
    session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not _valid_session_id(session_id):
        session_id = secrets.token_urlsafe(32)
    return session_id, _csrf_token(session_id)


def validate_csrf(request: Request) -> JSONResponse | None:
    session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
    supplied_token = request.headers.get("x-csrf-token", "")
    if (
        not _valid_session_id(session_id)
        or not supplied_token
        or not hmac.compare_digest(supplied_token, _csrf_token(session_id))
    ):
        return _api_error(403, "csrf_failed", "A valid local CSRF token is required.")
    return None


class LocalRequestSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method in MUTATING_METHODS:
            origin_error = validate_request_origin(request)
            if origin_error is not None:
                return origin_error
            csrf_error = validate_csrf(request)
            if csrf_error is not None:
                return csrf_error

        response = await call_next(request)
        path = request.url.path
        if path == API_PREFIX or path.startswith(f"{API_PREFIX}/"):
            if (
                response.status_code >= 400
                and "application/json" not in response.headers.get("content-type", "")
            ):
                return _api_error(
                    response.status_code,
                    "http_error",
                    "API request failed.",
                )
            response.headers["Cache-Control"] = "no-store"
        return response
