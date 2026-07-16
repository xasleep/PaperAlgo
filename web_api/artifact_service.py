import os
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .config import RUNS_DIR
from .errors import (
    BinaryFileNotSupportedError,
    FileTooLargeError,
    InvalidRepoPathError,
    JobNotFoundError,
    RepoFileNotFoundError,
    RepoNotAvailableError,
    TextEncodingNotSupportedError,
    UnsupportedFileTypeError,
)
from .json_io import JSON_READ_ERROR_KEY, read_json_file
from .job_service import build_job_view
from .path_security import validate_job_id


MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
TEXT_FILE_EXTENSIONS = {
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".log",
    ".md",
    ".py",
    ".r",
    ".rst",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
BINARY_SAMPLE_BYTES = 8192


def get_run_dir(job_id: str) -> Path:
    return RUNS_DIR / validate_job_id(job_id)


def get_repo_dir(job_id: str) -> Path:
    return get_run_dir(job_id) / "repo"


def ensure_job_exists(job_id: str) -> Path:
    run_dir = get_run_dir(job_id)
    if not run_dir.exists() or not run_dir.is_dir():
        raise JobNotFoundError()
    return run_dir


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    if not RUNS_DIR.exists():
        return []

    jobs = []
    try:
        run_dirs = RUNS_DIR.iterdir()
    except OSError:
        return []

    for run_dir in run_dirs:
        try:
            if not run_dir.is_dir():
                continue
        except OSError:
            continue
        try:
            status = read_json_file(
                run_dir / "run_status.json",
                default={},
                include_error=True,
            )
            summary = read_json_file(
                run_dir / "run_summary.json",
                default={},
                include_error=True,
            )
            status_read_error = status.pop(JSON_READ_ERROR_KEY, None)
            summary_read_error = summary.pop(JSON_READ_ERROR_KEY, None)
            mtime = _job_mtime(run_dir)
            job = build_job_view(
                run_dir.name,
                run_dir=run_dir,
                status=status,
                summary=summary,
                status_read_error=status_read_error,
                summary_read_error=summary_read_error,
            )
            job["mtime"] = mtime
            jobs.append(job)
        except OSError:
            continue

    jobs.sort(key=lambda item: item["mtime"], reverse=True)
    for job in jobs:
        job.pop("mtime", None)
    return jobs[: max(1, min(limit, 200))]


def _job_mtime(run_dir: Path) -> float:
    mtimes = []
    for path in [run_dir / "run_status.json", run_dir / "run_summary.json"]:
        try:
            if path.exists():
                mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    if mtimes:
        return max(mtimes)
    try:
        return run_dir.stat().st_mtime
    except OSError:
        return 0.0


def artifact_summary(job_id: str) -> dict[str, Any]:
    run_dir = ensure_job_exists(job_id)
    repo_dir = run_dir / "repo"
    results_dir = run_dir / "results"
    logs_dir = run_dir / "logs"

    def file_count(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(1 for item in path.rglob("*") if item.is_file())

    return {
        "job_id": job_id,
        "run_dir": str(run_dir),
        "repo_dir": str(repo_dir),
        "results_dir": str(results_dir),
        "logs_dir": str(logs_dir),
        "repo_file_count": file_count(repo_dir),
        "result_file_count": file_count(results_dir),
        "log_file_count": file_count(logs_dir),
        "repo_ready": repo_dir.exists() and any(repo_dir.iterdir()),
    }


def safe_repo_path(job_id: str, relative_path: str = "") -> Path:
    ensure_job_exists(job_id)
    repo_dir = get_repo_dir(job_id).resolve()
    if not repo_dir.exists() or not repo_dir.is_dir():
        raise RepoNotAvailableError()

    clean_path = (relative_path or "").replace("\\", "/").strip("/")
    if clean_path.startswith("/") or ".." in Path(clean_path).parts:
        raise InvalidRepoPathError()

    target = (repo_dir / clean_path).resolve()
    if target != repo_dir and repo_dir not in target.parents:
        raise InvalidRepoPathError()
    return target


def repo_tree(job_id: str, max_files: int = 500) -> list[dict[str, Any]]:
    repo_dir = safe_repo_path(job_id)
    entries = []
    for path in sorted(repo_dir.rglob("*")):
        relative_path = path.relative_to(repo_dir).as_posix()
        entries.append(
            {
                "path": relative_path,
                "name": path.name,
                "type": "directory" if path.is_dir() else "file",
                "size": path.stat().st_size if path.is_file() else None,
                "modified_at": path.stat().st_mtime,
            }
        )
        if len(entries) >= max_files:
            break
    return entries


def read_repo_file(job_id: str, relative_path: str) -> dict[str, Any]:
    target = safe_repo_path(job_id, relative_path)
    if not target.exists() or not target.is_file():
        raise RepoFileNotFoundError()
    size = target.stat().st_size
    if target.suffix.lower() not in TEXT_FILE_EXTENSIONS:
        raise UnsupportedFileTypeError("File type is not supported for text preview.")
    if size > MAX_TEXT_FILE_BYTES:
        raise FileTooLargeError("File is too large to display as text.")
    data = target.read_bytes()
    if _looks_binary(data[:BINARY_SAMPLE_BYTES]):
        raise BinaryFileNotSupportedError("Binary files are not supported for text preview.")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TextEncodingNotSupportedError()
    return {
        "job_id": job_id,
        "path": relative_path.replace("\\", "/").strip("/"),
        "size": size,
        "content": content,
    }


def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    if not data:
        return False
    control_bytes = sum(
        1
        for byte in data
        if byte < 32 and byte not in {9, 10, 12, 13}
    )
    return control_bytes / len(data) > 0.05


def make_repo_zip(job_id: str) -> Path:
    repo_dir = safe_repo_path(job_id)
    downloads_dir = get_run_dir(job_id) / ".downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    final_path = downloads_dir / f"{job_id}_repo_{uuid.uuid4().hex}.zip"
    temp_path = downloads_dir / f".{final_path.name}.{os.getpid()}.tmp"
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(repo_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(repo_dir).as_posix())
        os.replace(temp_path, final_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return final_path
