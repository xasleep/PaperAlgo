import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, TextIO

from .config import CODES_DIR, MAX_PDF_UPLOAD_BYTES, REPO_ROOT, RUNS_DIR, UPLOADS_DIR
from .errors import (
    FileTooLargeError,
    InvalidParameterError,
    InvalidUploadError,
    JobNotCancelableError,
    JobNotFoundError,
    UnsupportedFileTypeError,
)
from .json_io import JSON_READ_ERROR_KEY, read_json_file, write_json_file_atomic
from .path_security import path_is_under, validate_job_id
from .schemas import ConsoleOutput, DomainName, EvalType, WebSettings


ACTIVE_PROCESSES: dict[str, subprocess.Popen] = {}
ACTIVE_LOG_FILES: dict[str, TextIO] = {}
PDF_MAGIC = b"%PDF-"
UPLOAD_CHUNK_BYTES = 1024 * 1024
UPLOAD_FILE_NAME = "document.pdf"
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
TERMINAL_STATUSES = {"completed", "failed", "canceled"}
ACTIVE_STATUSES = {"starting", "queued", "running"}
JOB_STATUS_MAP = {
    "starting": "queued",
    "queued": "queued",
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "canceled": "canceled",
}
SYSTEM_ENV_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
}
PROVIDER_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "MOONSHOT_API_KEY",
    "MOONSHOT_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
}


def compact_now_str() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sanitize_name(value: str, fallback: str) -> str:
    value = value.strip() or fallback
    value = Path(value).stem if value.lower().endswith(".pdf") else value
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("._") or fallback


def write_json_file(path: Path, data: dict) -> None:
    write_json_file_atomic(path, data)


def update_status(run_dir: Path, **updates: object) -> dict:
    status_path = run_dir / "run_status.json"
    current = read_json_file(status_path, default={})
    current.update(updates)
    current["updated_at"] = now_str()
    write_json_file(status_path, current)
    return current


def make_job_id(paper_name: str) -> str:
    return f"{compact_now_str()}_{paper_name}_{uuid.uuid4().hex[:8]}"


def make_upload_id() -> str:
    return uuid.uuid4().hex


def validate_upload_id(upload_id: str) -> str:
    if not isinstance(upload_id, str) or not UPLOAD_ID_RE.fullmatch(upload_id):
        raise InvalidParameterError(
            "upload_id is invalid.",
            details={"parameter": "upload_id"},
        )
    return upload_id


def upload_path(upload_id: str) -> Path:
    upload_id = validate_upload_id(upload_id)
    root = UPLOADS_DIR.resolve(strict=False)
    candidate = (UPLOADS_DIR / upload_id / UPLOAD_FILE_NAME).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise InvalidParameterError(
            "upload_id is invalid.",
            details={"parameter": "upload_id"},
        ) from exc
    return candidate


def resolve_upload(upload_id: str) -> Path:
    candidate = upload_path(upload_id)
    try:
        path_stat = candidate.lstat()
    except FileNotFoundError as exc:
        raise InvalidParameterError(
            "upload_id does not reference an available PDF.",
            details={"parameter": "upload_id"},
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_nlink > 1
        or getattr(path_stat, "st_file_attributes", 0) & reparse_flag
    ):
        raise InvalidParameterError(
            "upload_id does not reference a safe PDF.",
            details={"parameter": "upload_id"},
        )
    return candidate


def save_upload(
    upload_id: str,
    filename: str,
    source: BinaryIO,
    *,
    max_bytes: int | None = None,
) -> Path:
    upload_id = validate_upload_id(upload_id)
    if not filename or not filename.lower().endswith(".pdf"):
        raise UnsupportedFileTypeError("Only PDF uploads are supported.")
    limit = MAX_PDF_UPLOAD_BYTES if max_bytes is None else max_bytes
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < len(PDF_MAGIC):
        raise ValueError("PDF upload limit must be a positive integer large enough for the header.")

    upload_dir = UPLOADS_DIR / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    target_path = upload_path(upload_id)
    temp_path = upload_dir / f".{uuid.uuid4().hex}.tmp"
    total_bytes = 0
    header = b""
    try:
        with open(temp_path, "xb") as out:
            while True:
                chunk = source.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise InvalidUploadError("Upload stream did not return bytes.")
                if len(header) < len(PDF_MAGIC):
                    needed = len(PDF_MAGIC) - len(header)
                    header += chunk[:needed]
                    if len(header) == len(PDF_MAGIC) and header != PDF_MAGIC:
                        raise InvalidUploadError("Upload content is not a valid PDF file.")
                total_bytes += len(chunk)
                if total_bytes > limit:
                    raise FileTooLargeError(
                        f"PDF upload exceeds the configured limit of {limit} bytes."
                    )
                out.write(chunk)
            if header != PDF_MAGIC:
                raise InvalidUploadError("Upload content is not a valid PDF file.")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp_path, target_path)
        return target_path
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            if upload_dir.exists() and not any(upload_dir.iterdir()):
                upload_dir.rmdir()
        except OSError:
            pass


def validate_pdf_markdown_path(pdf_markdown_path: str) -> Path:
    raw_path = (pdf_markdown_path or "").strip()
    if not raw_path:
        raise InvalidParameterError(
            "pdf_markdown_path is required when skip_mineru is true.",
            details={"parameter": "pdf_markdown_path"},
        )

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate

    try:
        resolved_path = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise InvalidParameterError(
            "pdf_markdown_path must point to an existing Markdown file.",
            details={"parameter": "pdf_markdown_path"},
        )

    if not resolved_path.is_file():
        raise InvalidParameterError(
            "pdf_markdown_path must point to an existing Markdown file.",
            details={"parameter": "pdf_markdown_path"},
        )
    if resolved_path.suffix.lower() not in MARKDOWN_EXTENSIONS:
        raise InvalidParameterError(
            "pdf_markdown_path must use a Markdown extension.",
            details={"parameter": "pdf_markdown_path"},
        )

    runs_root = RUNS_DIR.resolve()
    if not path_is_under(resolved_path, runs_root):
        raise InvalidParameterError(
            "pdf_markdown_path must be under the runs directory.",
            details={"parameter": "pdf_markdown_path"},
        )

    return resolved_path


def build_pipeline_command(
    *,
    pdf_path: Path,
    job_id: str,
    paper_name: str,
    settings: WebSettings,
    domain: DomainName,
    eval_type: EvalType,
    generated_n: int,
    auto_refine: bool,
    max_repair_rounds: int,
    console_output: ConsoleOutput,
    skip_mineru: bool,
    pdf_markdown_path: str,
) -> list[str]:
    cmd = [
        sys.executable,
        str(CODES_DIR / "run_pipeline.py"),
        "--paper_pdf_path",
        str(pdf_path),
        "--paper_name",
        paper_name,
        "--domain",
        domain,
        "--reproduce_provider",
        settings.reproduce.provider,
        "--reproduce_gpt_version",
        settings.reproduce.model,
        "--eval_provider",
        settings.evaluation.provider,
        "--eval_gpt_version",
        settings.evaluation.model,
        "--runs_dir",
        str(RUNS_DIR),
        "--job_id",
        job_id,
        "--eval_type",
        eval_type,
        "--generated_n",
        str(generated_n),
        "--max_repair_rounds",
        str(max_repair_rounds),
        "--console_output",
        console_output,
    ]

    cmd.append("--auto_refine" if auto_refine else "--no-auto_refine")

    if settings.evaluation.fallback_models:
        cmd.extend(
            [
                "--eval_fallback_gpt_versions",
                ",".join(settings.evaluation.fallback_models),
            ]
        )

    if skip_mineru:
        pdf_markdown_path = str(validate_pdf_markdown_path(pdf_markdown_path))
        cmd.append("--skip_mineru")
        cmd.extend(["--pdf_markdown_path", pdf_markdown_path])

    return cmd


def build_pipeline_env(settings: WebSettings) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in SYSTEM_ENV_ALLOWLIST and name.upper() not in PROVIDER_ENV_NAMES
    }
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "REPRODUCE_API_KEY": settings.reproduce.api_key,
            "EVAL_API_KEY": settings.evaluation.api_key,
        }
    )
    reproduce_base_url = settings.reproduce.base_url.strip()
    eval_base_url = settings.evaluation.base_url.strip()
    if reproduce_base_url:
        env["REPRODUCE_BASE_URL"] = reproduce_base_url
    if eval_base_url:
        env["EVAL_BASE_URL"] = eval_base_url
    return env


def pipeline_popen_kwargs() -> dict[str, object]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def process_kill_strategy() -> str:
    return "windows_taskkill_tree" if sys.platform == "win32" else "posix_process_group"


def process_group_id(proc: subprocess.Popen) -> int:
    if sys.platform == "win32":
        return int(proc.pid)
    try:
        return int(os.getpgid(proc.pid))
    except OSError:
        return int(proc.pid)


def _coerce_pid(value: object) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def is_pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
        except ImportError:
            return False

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            int(pid),
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _wait_for_exit(proc: subprocess.Popen | None, pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    if proc is not None:
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            pass

    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.05)
    return not is_pid_alive(pid)


def terminate_process_tree(
    pid: int,
    proc: subprocess.Popen | None = None,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    if sys.platform == "win32":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if _wait_for_exit(proc, pid, timeout):
            return True, "Process tree terminated with taskkill."
        message = (result.stderr or result.stdout or "").strip()
        return False, message or "taskkill did not terminate the process tree."

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, "Process group was already gone."
    except OSError:
        if proc is not None:
            proc.terminate()

    if _wait_for_exit(proc, pid, timeout / 2):
        return True, "Process group terminated."

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, "Process group was already gone."
    except OSError:
        if proc is not None:
            proc.kill()

    if _wait_for_exit(proc, pid, timeout / 2):
        return True, "Process group killed."
    return False, "Process group did not terminate."


def _status_pid(status: dict, proc: subprocess.Popen | None = None) -> int | None:
    if proc is not None:
        return _coerce_pid(proc.pid)
    return (
        _coerce_pid(status.get("process_pid"))
        or _coerce_pid(status.get("pid"))
        or _coerce_pid(status.get("process_group_id"))
    )


def normalize_job_status(value: object) -> str:
    return JOB_STATUS_MAP.get(str(value or "").strip().lower(), "unknown")


def cancel_unavailable_reason(status: str, process_state: str) -> str:
    if process_state == "active":
        return ""
    if process_state == "detached":
        return "detached_after_restart"
    if process_state == "orphaned":
        return "orphaned_process"
    if process_state == "finished" or status in TERMINAL_STATUSES:
        return "already_finished"
    return "process_not_registered"


def sanitize_state_file_error(read_error: object) -> dict:
    if not isinstance(read_error, dict):
        return {}
    return {
        "file_name": str(read_error.get("file_name") or ""),
        "error": str(read_error.get("error") or ""),
    }


def build_job_view(
    job_id: str,
    *,
    run_dir: Path | None = None,
    status: dict | None = None,
    summary: dict | None = None,
    status_read_error: object = None,
    summary_read_error: object = None,
) -> dict:
    run_dir = run_dir or (RUNS_DIR / job_id)
    status = dict(status or {})
    summary = dict(summary or {})

    merged = dict(summary)
    merged.update(status)
    merged["job_id"] = job_id
    merged["run_dir"] = str(run_dir)

    raw_status = merged.get("status")
    process_status = apply_process_state(job_id, merged)
    normalized_status = normalize_job_status(raw_status)
    process_state = process_status.get("process_state") or "none"
    unavailable_reason = cancel_unavailable_reason(normalized_status, process_state)

    repo_status = merged.get("repo_status") or {}
    repo_status_value = repo_status.get("status") if isinstance(repo_status, dict) else repo_status
    eval_score = (
        repo_status.get("eval_score")
        if isinstance(repo_status, dict)
        else None
    )
    if eval_score is None:
        eval_score = merged.get("eval_score")

    if status_read_error:
        normalized_status = "unknown"
        if not merged.get("message"):
            process_status["message"] = "Job status file is unreadable."

    message = process_status.get("message") or summary.get("message")
    if (status_read_error or summary_read_error) and not message:
        message = "One or more job state files are unreadable."

    response = {
        "job_id": job_id,
        "paper_name": merged.get("paper_name"),
        "status": normalized_status,
        "process_state": process_state if process_state in {"active", "finished", "detached", "orphaned"} else "none",
        "cancelable": bool(process_status.get("process_state") == "active"),
        "cancel_unavailable_reason": unavailable_reason,
        "process_active": bool(process_status.get("process_active")),
        "stage": process_status.get("stage"),
        "message": message,
        "updated_at": merged.get("updated_at"),
        "repo_status": repo_status_value,
        "eval_score": eval_score,
        "run_dir": str(run_dir),
    }
    if merged.get("started_at") is not None:
        response["started_at"] = merged.get("started_at")
    if status_read_error:
        response["state_file_error"] = sanitize_state_file_error(status_read_error)
    elif summary_read_error:
        response["state_file_error"] = sanitize_state_file_error(summary_read_error)

    # Preserve existing low-level process fields for compatibility.
    for key in [
        "pid",
        "process_pid",
        "process_group_id",
        "process_kill_strategy",
        "return_code",
        "launcher_log",
    ]:
        if key in process_status:
            response[key] = process_status[key]

    return response


def apply_process_state(job_id: str, status: dict) -> dict:
    proc = ACTIVE_PROCESSES.get(job_id)
    pid = _status_pid(status, proc)
    if pid:
        status["pid"] = pid
        status["process_pid"] = pid

    current_status = status.get("status")
    if proc is not None and proc.poll() is None:
        status["process_state"] = "active"
        status["process_active"] = True
        status["cancelable"] = True
        status["pid"] = proc.pid
        status["process_pid"] = proc.pid
        return status

    status["process_active"] = False
    status["cancelable"] = False
    if current_status in TERMINAL_STATUSES:
        status["process_state"] = "finished"
    elif pid and is_pid_alive(pid):
        status["process_state"] = "detached"
        status.setdefault(
            "message",
            "Process is detached from this FastAPI server instance.",
        )
    elif current_status in {"starting", "queued"} and not pid:
        status["process_state"] = "none"
    elif current_status in ACTIVE_STATUSES or pid:
        status["process_state"] = "orphaned"
        status.setdefault("message", "Process is no longer running.")
    else:
        status["process_state"] = "none"
    return status


def _watch_process(job_id: str, proc: subprocess.Popen, log_file: TextIO, run_dir: Path) -> None:
    return_code = proc.wait()
    log_file.close()
    ACTIVE_PROCESSES.pop(job_id, None)
    ACTIVE_LOG_FILES.pop(job_id, None)

    status = read_json_file(run_dir / "run_status.json", default={})
    if return_code != 0 and status.get("status") not in {"failed", "canceled"}:
        update_status(
            run_dir,
            status="failed",
            stage="web_api_process",
            message=f"Pipeline process exited with code {return_code}.",
            return_code=return_code,
            process_state="finished",
            process_active=False,
            cancelable=False,
        )
    else:
        update_status(
            run_dir,
            return_code=return_code,
            process_state="finished",
            process_active=False,
            cancelable=False,
        )


def start_job(
    *,
    job_id: str,
    pdf_path: Path,
    paper_name: str,
    settings: WebSettings,
    domain: DomainName,
    eval_type: EvalType,
    generated_n: int,
    auto_refine: bool,
    max_repair_rounds: int,
    console_output: ConsoleOutput,
    skip_mineru: bool = False,
    pdf_markdown_path: str = "",
) -> dict:
    job_id = validate_job_id(job_id)
    if skip_mineru:
        pdf_markdown_path = str(validate_pdf_markdown_path(pdf_markdown_path))

    run_dir = RUNS_DIR / job_id
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    upload_path = pdf_path
    update_status(
        run_dir,
        status="starting",
        stage="web_api_starting",
        message="Starting pipeline from FastAPI.",
        job_id=job_id,
        paper_name=paper_name,
        run_directory=str(run_dir),
        pdf_path=str(upload_path),
        started_at=now_str(),
    )

    cmd = build_pipeline_command(
        pdf_path=upload_path,
        job_id=job_id,
        paper_name=paper_name,
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
    env = build_pipeline_env(settings)
    launcher_log = logs_dir / "00_web_api_pipeline.log"
    log_file = open(launcher_log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **pipeline_popen_kwargs(),
    )
    ACTIVE_PROCESSES[job_id] = proc
    ACTIVE_LOG_FILES[job_id] = log_file

    update_status(
        run_dir,
        status="running",
        stage="web_api_started",
        message="Pipeline process started.",
        pid=proc.pid,
        process_pid=proc.pid,
        process_group_id=process_group_id(proc),
        process_state="active",
        process_active=True,
        cancelable=True,
        process_kill_strategy=process_kill_strategy(),
        launcher_log=str(launcher_log),
    )
    threading.Thread(
        target=_watch_process,
        args=(job_id, proc, log_file, run_dir),
        daemon=True,
    ).start()

    return {
        "job_id": job_id,
        "status": "running",
        "run_dir": str(run_dir),
        "status_path": str(run_dir / "run_status.json"),
        "summary_path": str(run_dir / "run_summary.json"),
    }


def get_job_status(job_id: str) -> dict:
    job_id = validate_job_id(job_id)
    run_dir = RUNS_DIR / job_id
    if not run_dir.exists() or not run_dir.is_dir():
        raise JobNotFoundError()
    status = read_json_file(run_dir / "run_status.json", default={}, include_error=True)
    read_error = status.pop(JSON_READ_ERROR_KEY, None)
    if not status and not read_error:
        return build_job_view(
            job_id,
            run_dir=run_dir,
            status={
                "status": "unknown",
                "stage": "status_unavailable",
                "message": "Job status file is not available.",
            },
        )
    if read_error and not status:
        status = {"stage": "status_unavailable"}
    return build_job_view(
        job_id,
        run_dir=run_dir,
        status=status,
        status_read_error=read_error,
    )


def get_job_summary(job_id: str) -> dict:
    job_id = validate_job_id(job_id)
    run_dir = RUNS_DIR / job_id
    if not run_dir.exists() or not run_dir.is_dir():
        raise JobNotFoundError("Job summary not found.")
    summary = read_json_file(run_dir / "run_summary.json", default={}, include_error=True)
    read_error = summary.pop(JSON_READ_ERROR_KEY, None)
    if read_error:
        return {
            "job_id": job_id,
            "status": "unknown",
            "message": "Job summary file is unreadable.",
            "state_file_error": sanitize_state_file_error(read_error),
        }
    if not summary:
        return {
            "job_id": job_id,
            "status": "unknown",
            "message": "Job summary file is not available.",
        }
    return summary


def cancel_job(job_id: str) -> tuple[bool, str]:
    job_id = validate_job_id(job_id)
    run_dir = RUNS_DIR / job_id
    if not run_dir.exists() or not run_dir.is_dir():
        raise JobNotFoundError()

    status = get_job_status(job_id)
    if status["process_state"] != "active":
        reason = status.get("cancel_unavailable_reason") or "process_not_registered"
        if reason == "already_finished":
            raise JobNotCancelableError(reason, "Job is already finished and cannot be canceled.")
        raise JobNotCancelableError(reason)

    proc = ACTIVE_PROCESSES.get(job_id)
    if not proc or proc.poll() is not None:
        raise JobNotCancelableError("process_not_registered")

    ok, message = terminate_process_tree(proc.pid, proc)
    if not ok:
        update_status(
            run_dir,
            process_state="active",
            process_active=True,
            cancelable=True,
            message=f"Cancel failed: {message}",
        )
        raise JobNotCancelableError("process_not_registered", message)

    ACTIVE_PROCESSES.pop(job_id, None)
    update_status(
        run_dir,
        status="canceled",
        stage="canceled",
        message="Pipeline process tree was canceled from FastAPI.",
        canceled_at=now_str(),
        return_code=proc.poll(),
        process_state="finished",
        process_active=False,
        cancelable=False,
        process_kill_strategy=process_kill_strategy(),
    )
    return True, message
