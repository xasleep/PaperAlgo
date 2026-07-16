import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote

from .errors import InvalidJobIdError


_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def validate_job_id(job_id: str) -> str:
    raw_value = str(job_id or "")
    decoded_value = unquote(raw_value)

    for value in {raw_value, decoded_value}:
        if not value or value in {".", ".."}:
            raise InvalidJobIdError()
        if ".." in value:
            raise InvalidJobIdError()
        if "/" in value or "\\" in value or ":" in value:
            raise InvalidJobIdError()
        if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
            raise InvalidJobIdError()
        if not _SAFE_JOB_ID_RE.fullmatch(value):
            raise InvalidJobIdError()

    return decoded_value


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
