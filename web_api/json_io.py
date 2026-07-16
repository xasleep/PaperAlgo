import json
import os
import uuid
from pathlib import Path
from typing import Any


JSON_READ_ERROR_KEY = "_json_read_error"


def _copy_default(default: dict[str, Any] | None) -> dict[str, Any]:
    return dict(default) if isinstance(default, dict) else {}


def read_json_file(
    path: Path,
    default: dict[str, Any] | None = None,
    *,
    include_error: bool = False,
) -> dict[str, Any]:
    if not path.exists():
        return _copy_default(default)

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("JSON root must be an object.")
        return data
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        data = _copy_default(default)
        if include_error:
            data[JSON_READ_ERROR_KEY] = {
                "file_name": path.name,
                "error": type(exc).__name__,
            }
        return data


def write_json_file_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
