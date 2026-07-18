import os
import stat
import unicodedata
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

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
MAX_ARTIFACT_FILES = 512
MAX_ARTIFACT_FILE_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 128 * 1024 * 1024
MAX_TREE_ENTRIES = 2000
_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _has_reparse_flag(path_stat: os.stat_result) -> bool:
    return bool(getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT_FLAG)


def _validate_lstat(
    path: Path,
    path_stat: os.stat_result,
    *,
    require_directory: bool = False,
) -> None:
    if stat.S_ISLNK(path_stat.st_mode) or _has_reparse_flag(path_stat):
        raise InvalidRepoPathError(
            f"Artifact path contains a symlink, junction, or reparse point: {path.name}."
        )
    if require_directory and not stat.S_ISDIR(path_stat.st_mode):
        raise RepoNotAvailableError()
    if stat.S_ISREG(path_stat.st_mode) and path_stat.st_nlink > 1:
        raise InvalidRepoPathError(
            f"Artifact path contains a hard-linked file: {path.name}."
        )


def _validate_existing_chain(root: Path, target: Path) -> None:
    try:
        relative = target.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise InvalidRepoPathError() from exc

    current = root.absolute()
    try:
        root_stat = current.lstat()
    except FileNotFoundError:
        return
    _validate_lstat(current, root_stat, require_directory=True)
    for component in relative.parts:
        current = current / component
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            break
        _validate_lstat(current, current_stat)


def _ensure_real_directory(path: Path, *, missing_error: Exception) -> Path:
    try:
        path_stat = path.lstat()
    except FileNotFoundError as exc:
        raise missing_error from exc
    _validate_lstat(path, path_stat, require_directory=True)
    return path.absolute()


def _iter_safe_entries(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Walk an artifact root with lstat and without following directory links."""

    def walk(directory: Path) -> Iterator[tuple[Path, os.stat_result]]:
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(scanner, key=lambda item: item.name.casefold())
        except OSError as exc:
            raise InvalidRepoPathError(
                f"Artifact directory cannot be inspected safely: {directory.name}."
            ) from exc

        for entry in entries:
            path = Path(entry.path)
            try:
                path_stat = path.lstat()
            except OSError as exc:
                raise InvalidRepoPathError(
                    f"Artifact path cannot be inspected safely: {path.name}."
                ) from exc
            _validate_lstat(path, path_stat)
            if not stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISREG(path_stat.st_mode):
                raise InvalidRepoPathError(
                    f"Artifact path is not a regular file or directory: {path.name}."
                )
            yield path, path_stat
            if stat.S_ISDIR(path_stat.st_mode):
                yield from walk(path)

    yield from walk(root)


def _collect_safe_files(
    root: Path,
    *,
    max_files: int = MAX_ARTIFACT_FILES,
    max_file_bytes: int = MAX_ARTIFACT_FILE_BYTES,
    max_total_bytes: int = MAX_ARTIFACT_TOTAL_BYTES,
) -> list[tuple[Path, str, os.stat_result]]:
    if min(max_files, max_file_bytes, max_total_bytes) < 1:
        raise ValueError("Artifact limits must be positive integers.")

    files: list[tuple[Path, str, os.stat_result]] = []
    total_bytes = 0
    for path, path_stat in _iter_safe_entries(root):
        if not stat.S_ISREG(path_stat.st_mode):
            continue
        if len(files) >= max_files:
            raise FileTooLargeError(
                f"Artifact file count exceeds the configured limit of {max_files}."
            )
        if path_stat.st_size > max_file_bytes:
            raise FileTooLargeError(
                f"Artifact single-file size exceeds {max_file_bytes} bytes: {path.name}."
            )
        total_bytes += path_stat.st_size
        if total_bytes > max_total_bytes:
            raise FileTooLargeError(
                f"Artifact total size exceeds the configured limit of {max_total_bytes} bytes."
            )
        files.append((path, path.relative_to(root).as_posix(), path_stat))
    return files


def _same_open_file(before: os.stat_result, after: os.stat_result) -> bool:
    before_identity = (before.st_dev, before.st_ino)
    after_identity = (after.st_dev, after.st_ino)
    if before_identity != (0, 0) and after_identity != (0, 0):
        if before_identity != after_identity:
            return False
    return (
        stat.S_IFMT(before.st_mode) == stat.S_IFMT(after.st_mode)
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_nlink == after.st_nlink
    )


def _read_open_verified(
    path: Path,
    before: os.stat_result,
    *,
    max_bytes: int,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InvalidRepoPathError(
            f"Artifact file could not be opened safely: {path.name}."
        ) from exc
    try:
        opened_stat = os.fstat(descriptor)
        _validate_lstat(path, opened_stat)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise InvalidRepoPathError(
                f"Artifact file is no longer regular: {path.name}."
            )
        if not _same_open_file(before, opened_stat):
            before_identity = (before.st_dev, before.st_ino)
            opened_identity = (opened_stat.st_dev, opened_stat.st_ino)
            if (
                before_identity != (0, 0)
                and before_identity == opened_identity
                and opened_stat.st_size > before.st_size
            ):
                raise FileTooLargeError(
                    f"Artifact file grew after collection: {path.name}."
                )
            raise InvalidRepoPathError(
                f"Artifact file changed while it was being opened: {path.name}."
            )
        if opened_stat.st_size > max_bytes:
            raise FileTooLargeError(
                f"Artifact single-file size exceeds {max_bytes} bytes: {path.name}."
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        final_stat = os.fstat(descriptor)
        _validate_lstat(path, final_stat)
        if not _same_open_file(opened_stat, final_stat) or len(data) != final_stat.st_size:
            raise InvalidRepoPathError(
                f"Artifact file changed while it was being read: {path.name}."
            )
        if len(data) > max_bytes:
            raise FileTooLargeError(
                f"Artifact single-file size exceeds {max_bytes} bytes: {path.name}."
            )
        return data
    finally:
        os.close(descriptor)


def get_run_dir(job_id: str) -> Path:
    return RUNS_DIR / validate_job_id(job_id)


def get_repo_dir(job_id: str) -> Path:
    return get_run_dir(job_id) / "repo"


def get_report_dir(job_id: str) -> Path:
    return get_run_dir(job_id) / "results"


def ensure_job_exists(job_id: str) -> Path:
    run_dir = get_run_dir(job_id)
    return _ensure_real_directory(run_dir, missing_error=JobNotFoundError())


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
    results_dir = get_report_dir(job_id)
    logs_dir = run_dir / "logs"

    def file_count(path: Path) -> int:
        try:
            root = _ensure_real_directory(path, missing_error=RepoNotAvailableError())
        except RepoNotAvailableError:
            return 0
        count = 0
        for _, path_stat in _iter_safe_entries(root):
            if not stat.S_ISREG(path_stat.st_mode):
                continue
            count += 1
            if count > MAX_ARTIFACT_FILES:
                raise FileTooLargeError(
                    "Artifact file count exceeds the configured limit of "
                    f"{MAX_ARTIFACT_FILES}."
                )
        return count

    repo_count = file_count(repo_dir)

    return {
        "job_id": job_id,
        "run_dir": str(run_dir),
        "repo_dir": str(repo_dir),
        "results_dir": str(results_dir),
        "logs_dir": str(logs_dir),
        "repo_file_count": repo_count,
        "result_file_count": file_count(results_dir),
        "log_file_count": file_count(logs_dir),
        "repo_ready": repo_count > 0,
    }


def safe_repo_path(job_id: str, relative_path: str = "") -> Path:
    run_dir = ensure_job_exists(job_id)
    repo_dir = _ensure_real_directory(
        run_dir / "repo",
        missing_error=RepoNotAvailableError(),
    )

    if not isinstance(relative_path, str):
        raise InvalidRepoPathError()
    if relative_path == "":
        return repo_dir
    if (
        relative_path != relative_path.strip()
        or "\\" in relative_path
        or ":" in relative_path
        or relative_path.startswith("/")
        or any(unicodedata.category(char).startswith("C") for char in relative_path)
    ):
        raise InvalidRepoPathError()

    parts = tuple(relative_path.split("/"))
    if (
        any(not part or part in {".", ".."} for part in parts)
        or any(part.endswith((" ", ".")) for part in parts)
        or PurePosixPath(relative_path).is_absolute()
    ):
        raise InvalidRepoPathError()

    target = repo_dir.joinpath(*parts)
    _validate_existing_chain(repo_dir, target)
    resolved_repo = repo_dir.resolve(strict=True)
    resolved_target = target.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_repo)
    except ValueError as exc:
        raise InvalidRepoPathError() from exc
    return resolved_target


def repo_tree(job_id: str, max_files: int = 500) -> list[dict[str, Any]]:
    repo_dir = safe_repo_path(job_id)
    entries = []
    for path, path_stat in _iter_safe_entries(repo_dir):
        relative_path = path.relative_to(repo_dir).as_posix()
        is_directory = stat.S_ISDIR(path_stat.st_mode)
        entries.append(
            {
                "path": relative_path,
                "name": path.name,
                "type": "directory" if is_directory else "file",
                "size": None if is_directory else path_stat.st_size,
                "modified_at": path_stat.st_mtime,
            }
        )
        if len(entries) >= min(max_files, MAX_TREE_ENTRIES):
            break
    return entries


def read_repo_file(job_id: str, relative_path: str) -> dict[str, Any]:
    target = safe_repo_path(job_id, relative_path)
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        raise RepoFileNotFoundError()
    _validate_lstat(target, target_stat)
    if not stat.S_ISREG(target_stat.st_mode):
        raise RepoFileNotFoundError()
    if target.suffix.lower() not in TEXT_FILE_EXTENSIONS:
        raise UnsupportedFileTypeError("File type is not supported for text preview.")
    if target_stat.st_size > MAX_TEXT_FILE_BYTES:
        raise FileTooLargeError("File is too large to display as text.")
    data = _read_open_verified(target, target_stat, max_bytes=MAX_TEXT_FILE_BYTES)
    if _looks_binary(data[:BINARY_SAMPLE_BYTES]):
        raise BinaryFileNotSupportedError("Binary files are not supported for text preview.")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TextEncodingNotSupportedError()
    return {
        "job_id": job_id,
        "path": relative_path,
        "size": len(data),
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


def make_repo_zip(
    job_id: str,
    *,
    max_files: int = MAX_ARTIFACT_FILES,
    max_file_bytes: int = MAX_ARTIFACT_FILE_BYTES,
    max_total_bytes: int = MAX_ARTIFACT_TOTAL_BYTES,
) -> Path:
    repo_dir = safe_repo_path(job_id)
    files = _collect_safe_files(
        repo_dir,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    run_dir = ensure_job_exists(job_id)
    downloads_dir = run_dir / ".downloads"
    _validate_existing_chain(run_dir, downloads_dir)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    _ensure_real_directory(downloads_dir, missing_error=RepoNotAvailableError())
    final_path = downloads_dir / f"{job_id}_repo_{uuid.uuid4().hex}.zip"
    temp_path = downloads_dir / f".{final_path.name}.{os.getpid()}.tmp"
    try:
        actual_total_bytes = 0
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path, relative_path, path_stat in files:
                _validate_existing_chain(repo_dir, path)
                data = _read_open_verified(path, path_stat, max_bytes=max_file_bytes)
                actual_total_bytes += len(data)
                if actual_total_bytes > max_total_bytes:
                    raise FileTooLargeError(
                        "Artifact actual total size exceeds the configured limit of "
                        f"{max_total_bytes} bytes."
                    )
                zf.writestr(relative_path, data)
        os.replace(temp_path, final_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return final_path
