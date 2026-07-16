from pathlib import Path

from .config import RUNS_DIR
from .errors import InvalidRepoPathError, JobNotFoundError, LogNotFoundError
from .path_security import validate_job_id


LOG_TAIL_CHUNK_BYTES = 8192
MAX_LOG_TAIL_BYTES = 2 * 1024 * 1024


def get_run_dir(job_id: str) -> Path:
    return RUNS_DIR / validate_job_id(job_id)


def get_logs_dir(job_id: str) -> Path:
    return get_run_dir(job_id) / "logs"


def list_logs(job_id: str) -> list[str]:
    run_dir = get_run_dir(job_id)
    if not run_dir.exists() or not run_dir.is_dir():
        raise JobNotFoundError()
    logs_dir = get_logs_dir(job_id)
    if not logs_dir.exists():
        return []
    return sorted(
        path.name for path in logs_dir.iterdir() if path.is_file() and path.suffix == ".log"
    )


def safe_log_path(job_id: str, file_name: str) -> Path:
    logs_dir = get_logs_dir(job_id).resolve()
    if Path(file_name).name != file_name or Path(file_name).suffix != ".log":
        raise InvalidRepoPathError("Log file name is invalid.")
    target = (logs_dir / file_name).resolve()
    if logs_dir not in target.parents and target != logs_dir:
        raise InvalidRepoPathError("Log file name is invalid.")
    if not target.exists() or not target.is_file():
        raise LogNotFoundError()
    return target


def read_log(job_id: str, file_name: str, tail_lines: int = 200) -> str:
    target = safe_log_path(job_id, file_name)
    tail_lines = max(1, min(tail_lines, 2000))
    chunks: list[bytes] = []
    newline_count = 0
    bytes_read = 0
    with open(target, "rb") as f:
        f.seek(0, 2)
        position = f.tell()
        while position > 0 and newline_count <= tail_lines and bytes_read < MAX_LOG_TAIL_BYTES:
            read_size = min(LOG_TAIL_CHUNK_BYTES, position, MAX_LOG_TAIL_BYTES - bytes_read)
            position -= read_size
            f.seek(position)
            chunk = f.read(read_size)
            chunks.append(chunk)
            bytes_read += len(chunk)
            newline_count += chunk.count(b"\n")

    data = b"".join(reversed(chunks))
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    return "".join(lines[-tail_lines:])
