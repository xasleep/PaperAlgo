"""Trusted boundary for model-generated task file paths.

Planning converts its untrusted ``list[str]`` exactly once and persists a
versioned manifest. Consumers reload that manifest through the same validation
boundary and use immutable :class:`TaskFile` objects for filesystem access.

The current Pipeline deliberately uses :data:`DEFAULT_TASK_MANIFEST_POLICY`.
Low-level callers may pass another trusted policy explicitly, but they must use
the same policy when creating and loading a manifest. Serialized manifests do
not self-declare policy and cannot relax the caller's security boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


MAX_TASK_FILES = 64
MAX_RELATIVE_PATH_LENGTH = 180
MAX_PATH_DEPTH = 8
MAX_COMPONENT_LENGTH = 100
TASK_MANIFEST_VERSION = 1
TASK_MANIFEST_FILENAME = "task_manifest.json"

ALLOWED_TASK_EXTENSIONS = frozenset(
    {
        ".py",
        ".r",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".sh",
        ".ps1",
        ".bat",
        ".cmd",
        ".md",
        ".markdown",
        ".txt",
    }
)

_WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")
_SAFE_ARTIFACT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TASK_FILE_VALIDATION_TOKEN = object()


class TaskManifestError(ValueError):
    """Base error for an invalid model-generated task manifest."""


class InvalidTaskPathError(TaskManifestError):
    """Raised when a task path is syntactically or semantically unsafe."""


class DuplicateTaskPathError(TaskManifestError):
    """Raised when paths collide under Windows-compatible normalization."""


class TaskManifestLimitError(TaskManifestError):
    """Raised when a manifest exceeds a configured resource limit."""


class UnsafeTaskWriteError(TaskManifestError):
    """Raised when a validated task path cannot be accessed safely."""


@dataclass(frozen=True, slots=True)
class TaskManifestPolicy:
    """Caller-supplied bounds; not a Web or Pipeline runtime setting."""

    max_files: int = MAX_TASK_FILES
    max_relative_path_length: int = MAX_RELATIVE_PATH_LENGTH
    max_path_depth: int = MAX_PATH_DEPTH
    max_component_length: int = MAX_COMPONENT_LENGTH
    allowed_extensions: frozenset[str] = ALLOWED_TASK_EXTENSIONS

    def __post_init__(self) -> None:
        for field_name in (
            "max_files",
            "max_relative_path_length",
            "max_path_depth",
            "max_component_length",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise TaskManifestLimitError(f"{field_name} must be a positive integer.")
        if not isinstance(self.allowed_extensions, frozenset) or not self.allowed_extensions:
            raise TaskManifestError("allowed_extensions must be a non-empty frozenset.")
        if any(
            not isinstance(extension, str)
            or not extension.startswith(".")
            or extension != extension.lower()
            for extension in self.allowed_extensions
        ):
            raise TaskManifestError(
                "allowed_extensions must contain lowercase dot-prefixed strings."
            )


DEFAULT_TASK_MANIFEST_POLICY = TaskManifestPolicy()


@dataclass(frozen=True, slots=True)
class TaskFile:
    """An immutable, validated POSIX path from a task manifest."""

    relative_path: str
    parts: tuple[str, ...]
    canonical_key: str
    _validation_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._validation_token is not _TASK_FILE_VALIDATION_TOKEN:
            raise InvalidTaskPathError(
                "TaskFile objects must be created by validate_task_path()."
            )


@dataclass(frozen=True, slots=True)
class TaskManifest:
    """Versioned files plus the trusted policy used to validate them."""

    files: tuple[TaskFile, ...]
    version: int = TASK_MANIFEST_VERSION
    policy: TaskManifestPolicy = DEFAULT_TASK_MANIFEST_POLICY

    def __post_init__(self) -> None:
        if self.version != TASK_MANIFEST_VERSION:
            raise TaskManifestError(
                f"Unsupported TaskManifest version {self.version!r}; "
                f"expected {TASK_MANIFEST_VERSION}."
            )
        if not isinstance(self.files, tuple) or any(
            not isinstance(task_file, TaskFile) for task_file in self.files
        ):
            raise TaskManifestError("TaskManifest files must be validated TaskFile objects.")
        if not self.files:
            raise TaskManifestError("TaskManifest must contain at least one validated file.")
        if not isinstance(self.policy, TaskManifestPolicy):
            raise TaskManifestError("TaskManifest policy must be a trusted TaskManifestPolicy.")

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(task.relative_path for task in self.files)

    def find(self, task_file: TaskFile) -> TaskFile | None:
        """Return the original manifest object matching a validated path."""

        _require_task_file(task_file)
        for manifest_file in self.files:
            if manifest_file.canonical_key == task_file.canonical_key:
                return manifest_file
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "files": [{"path": task_file.relative_path} for task_file in self.files],
        }


def _canonical_path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _path_error(raw_path: object, reason: str) -> InvalidTaskPathError:
    return InvalidTaskPathError(f"Rejected task path {raw_path!r}: {reason}.")


def validate_task_path(
    raw_path: object,
    *,
    policy: TaskManifestPolicy = DEFAULT_TASK_MANIFEST_POLICY,
) -> TaskFile:
    """Validate one untrusted Planning path without rewriting or repairing it."""

    if not isinstance(policy, TaskManifestPolicy):
        raise TaskManifestError("Task path validation requires a TaskManifestPolicy.")
    if not isinstance(raw_path, str):
        raise _path_error(raw_path, "path must be a string")
    if not raw_path:
        raise _path_error(raw_path, "path must not be empty")
    if raw_path != raw_path.strip():
        raise _path_error(raw_path, "leading or trailing whitespace is not allowed")
    if any(unicodedata.category(char).startswith("C") for char in raw_path):
        raise _path_error(raw_path, "NUL, control, and format characters are not allowed")
    if "\\" in raw_path:
        raise _path_error(raw_path, "backslashes and UNC paths are not allowed")
    if _DRIVE_PATH_RE.match(raw_path):
        raise _path_error(raw_path, "Windows drive paths are not allowed")
    if ":" in raw_path:
        raise _path_error(raw_path, "URI schemes, drive paths, and NTFS ADS are not allowed")
    if raw_path.startswith("/") or PurePosixPath(raw_path).is_absolute():
        raise _path_error(raw_path, "absolute paths are not allowed")
    if raw_path.endswith("/"):
        raise _path_error(raw_path, "trailing slashes are not allowed")
    if len(raw_path) > policy.max_relative_path_length:
        raise TaskManifestLimitError(
            f"Task path exceeds {policy.max_relative_path_length} characters: {raw_path!r}."
        )

    parts = tuple(raw_path.split("/"))
    if any(not component for component in parts):
        raise _path_error(raw_path, "empty path components are not allowed")
    if len(parts) > policy.max_path_depth:
        raise TaskManifestLimitError(
            f"Task path exceeds maximum depth {policy.max_path_depth}: {raw_path!r}."
        )

    for component in parts:
        if component in {".", ".."}:
            raise _path_error(raw_path, "dot path components are not allowed")
        if len(component) > policy.max_component_length:
            raise TaskManifestLimitError(
                f"Task path component exceeds {policy.max_component_length} characters: "
                f"{component!r}."
            )
        if component.endswith((" ", ".")):
            raise _path_error(raw_path, "components may not end in a space or period")
        invalid = sorted(set(component) & _WINDOWS_INVALID_CHARS)
        if invalid:
            raise _path_error(
                raw_path,
                f"Windows-invalid character {invalid[0]!r} is not allowed",
            )
        reserved_stem = component.split(".", 1)[0].upper()
        if reserved_stem in _WINDOWS_RESERVED_NAMES:
            raise _path_error(raw_path, f"Windows reserved name {reserved_stem!r} is not allowed")

    extension = PurePosixPath(raw_path).suffix.lower()
    if extension not in policy.allowed_extensions:
        raise _path_error(raw_path, f"extension {extension or '<none>'!r} is not allowed")

    return TaskFile(
        relative_path=raw_path,
        parts=parts,
        canonical_key=_canonical_path_key(raw_path),
        _validation_token=_TASK_FILE_VALIDATION_TOKEN,
    )


def parse_task_manifest(
    raw_task_list: object,
    *,
    policy: TaskManifestPolicy = DEFAULT_TASK_MANIFEST_POLICY,
) -> TaskManifest:
    """Convert the current Planning ``list[str]`` into a trusted manifest."""

    if not isinstance(raw_task_list, list):
        raise TaskManifestError("Task list must be a list of file path strings.")
    if not isinstance(policy, TaskManifestPolicy):
        raise TaskManifestError("Task manifest parsing requires a TaskManifestPolicy.")
    if not raw_task_list:
        raise TaskManifestError("Task list must contain at least one file path.")
    if len(raw_task_list) > policy.max_files:
        raise TaskManifestLimitError(
            f"Task list contains {len(raw_task_list)} files; maximum is {policy.max_files}."
        )

    files: list[TaskFile] = []
    seen: dict[str, str] = {}
    for raw_path in raw_task_list:
        task_file = validate_task_path(raw_path, policy=policy)
        previous = seen.get(task_file.canonical_key)
        if previous is not None:
            raise DuplicateTaskPathError(
                "Task paths collide after Unicode NFC and case-insensitive "
                f"normalization: {previous!r} and {task_file.relative_path!r}."
            )
        seen[task_file.canonical_key] = task_file.relative_path
        files.append(task_file)

    return TaskManifest(
        files=tuple(files),
        version=TASK_MANIFEST_VERSION,
        policy=policy,
    )


def parse_task_manifest_mapping(
    task_data: object,
    *,
    policy: TaskManifestPolicy = DEFAULT_TASK_MANIFEST_POLICY,
) -> TaskManifest:
    """Extract and validate the Task list from untrusted Planning output."""

    if not isinstance(task_data, dict):
        raise TaskManifestError("Planning task data must be a JSON object.")
    for key in ("Task list", "task_list", "task list"):
        if key in task_data:
            return parse_task_manifest(task_data[key], policy=policy)
    raise TaskManifestError(
        "Task list does not exist in Planning output; re-generate the planning."
    )


def parse_task_manifest_document(
    raw_document: object,
    *,
    policy: TaskManifestPolicy = DEFAULT_TASK_MANIFEST_POLICY,
) -> TaskManifest:
    """Revalidate a persisted versioned manifest without trusting its strings."""

    if not isinstance(raw_document, dict):
        raise TaskManifestError("TaskManifest document must be a JSON object.")
    version = raw_document.get("version")
    if version != TASK_MANIFEST_VERSION:
        raise TaskManifestError(
            f"Unsupported TaskManifest version {version!r}; expected {TASK_MANIFEST_VERSION}."
        )
    raw_files = raw_document.get("files")
    if not isinstance(raw_files, list):
        raise TaskManifestError("TaskManifest files must be a list.")
    raw_paths: list[object] = []
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"path"}:
            raise TaskManifestError(
                "Each TaskManifest file must be an object containing only 'path'."
            )
        raw_paths.append(item["path"])
    return parse_task_manifest(raw_paths, policy=policy)


def validate_repair_paths(
    raw_paths: object,
    manifest: TaskManifest,
) -> tuple[TaskFile, ...]:
    """Validate evaluation-selected repair files and bind them to the manifest."""

    if not isinstance(manifest, TaskManifest):
        raise TaskManifestError("Repair path validation requires a TaskManifest.")
    if not isinstance(raw_paths, list):
        raise TaskManifestError("files_to_repair must be a list of task paths.")

    selected: list[TaskFile] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        candidate = validate_task_path(raw_path, policy=manifest.policy)
        manifest_file = manifest.find(candidate)
        if manifest_file is None:
            raise InvalidTaskPathError(
                f"Rejected repair path {candidate.relative_path!r}: "
                "path is not present in the TaskManifest."
            )
        if manifest_file.canonical_key not in seen:
            selected.append(manifest_file)
            seen.add(manifest_file.canonical_key)
    return tuple(selected)


def _require_task_file(value: object) -> TaskFile:
    if not isinstance(value, TaskFile):
        raise InvalidTaskPathError(
            "Filesystem access requires a validated TaskFile; raw path strings are rejected."
        )
    return value


def _is_reparse_point(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _check_existing_path_chain(path: Path) -> None:
    absolute_path = path.absolute()
    parts = absolute_path.parts
    if not parts:
        return

    current = Path(parts[0])
    if _is_reparse_point(current):
        raise UnsafeTaskWriteError(f"Path contains a link or reparse point: {current}.")
    for component in parts[1:]:
        current = current / component
        if _is_reparse_point(current):
            raise UnsafeTaskWriteError(f"Path contains a link or reparse point: {current}.")


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def safe_join(root: os.PathLike[str] | str, validated_relative_path: TaskFile) -> Path:
    """Resolve a validated task path beneath ``root`` and reject link escapes."""

    task_file = _require_task_file(validated_relative_path)
    raw_root = Path(root).expanduser().absolute()
    _check_existing_path_chain(raw_root)
    resolved_root = raw_root.resolve(strict=False)

    candidate = raw_root.joinpath(*task_file.parts)
    _check_existing_path_chain(candidate)
    resolved_candidate = candidate.resolve(strict=False)
    if not _is_within(resolved_candidate, resolved_root):
        raise UnsafeTaskWriteError(
            f"Validated task path escapes its filesystem root: {task_file.relative_path!r}."
        )

    _check_existing_path_chain(resolved_candidate)
    if resolved_candidate.exists() and _is_reparse_point(resolved_candidate):
        raise UnsafeTaskWriteError(
            f"Task target is a link or reparse point: {task_file.relative_path!r}."
        )
    return resolved_candidate


def read_manifest_text_files(
    root: os.PathLike[str] | str,
    manifest: TaskManifest,
    *,
    allowed_extensions: set[str] | frozenset[str] | None = None,
) -> dict[str, str]:
    """Read existing UTF-8 files selected only by a validated manifest."""

    if not isinstance(manifest, TaskManifest):
        raise TaskManifestError("Manifest file reads require a TaskManifest.")
    normalized_extensions = None
    if allowed_extensions is not None:
        normalized_extensions = {extension.lower() for extension in allowed_extensions}

    files: dict[str, str] = {}
    for task_file in manifest.files:
        extension = PurePosixPath(task_file.relative_path).suffix.lower()
        if normalized_extensions is not None and extension not in normalized_extensions:
            continue
        target = safe_join(root, task_file)
        if not target.exists():
            continue
        if not target.is_file():
            raise UnsafeTaskWriteError(
                f"Task target is not a regular file: {task_file.relative_path!r}."
            )
        files[task_file.relative_path] = target.read_text(encoding="utf-8")
    return files


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # The file itself was fsynced before the atomic replace. Some
        # filesystems do not permit directory handles, so this is best effort.
        return


def safe_write_text(
    root: os.PathLike[str] | str,
    validated_relative_path: TaskFile,
    content: str,
) -> Path:
    """Atomically write UTF-8 text without allowing paths outside ``root``."""

    task_file = _require_task_file(validated_relative_path)
    if not isinstance(content, str):
        raise UnsafeTaskWriteError("Task file content must be text.")

    target = safe_join(root, task_file)
    temp_path: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target = safe_join(root, task_file)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        safe_join(root, task_file)
        os.replace(temp_path, target)
        temp_path = None
        _sync_directory(target.parent)
        return target
    except (InvalidTaskPathError, UnsafeTaskWriteError):
        raise
    except OSError as exc:
        raise UnsafeTaskWriteError(
            f"Failed to write task file {task_file.relative_path!r}: {exc}"
        ) from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def save_task_manifest(
    output_dir: os.PathLike[str] | str,
    manifest: TaskManifest,
) -> Path:
    """Persist files/version only; the trusted policy remains caller-owned."""

    if not isinstance(manifest, TaskManifest):
        raise TaskManifestError("Only a validated TaskManifest can be saved.")
    manifest_file = validate_task_path(TASK_MANIFEST_FILENAME)
    payload = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n"
    return safe_write_text(output_dir, manifest_file, payload)


def load_task_manifest(
    output_dir: os.PathLike[str] | str,
    *,
    policy: TaskManifestPolicy = DEFAULT_TASK_MANIFEST_POLICY,
) -> TaskManifest:
    """Load and revalidate using the caller's explicit trusted policy."""

    manifest_file = validate_task_path(TASK_MANIFEST_FILENAME)
    manifest_path = safe_join(output_dir, manifest_file)
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            raw_document = json.load(stream)
    except json.JSONDecodeError as exc:
        raise TaskManifestError(f"TaskManifest is not valid JSON: {exc}") from exc
    return parse_task_manifest_document(raw_document, policy=policy)


def task_artifact_key(validated_relative_path: TaskFile) -> str:
    """Return a stable ASCII artifact key without path-flattening collisions."""

    task_file = _require_task_file(validated_relative_path)
    basename = task_file.parts[-1]
    ascii_basename = (
        unicodedata.normalize("NFKD", basename).encode("ascii", "ignore").decode("ascii")
    )
    safe_basename = _SAFE_ARTIFACT_RE.sub("_", ascii_basename).strip("._-")
    if not safe_basename:
        safe_basename = "task"
    # Keep room for the hash and the longest current artifact suffix while
    # remaining below MAX_COMPONENT_LENGTH.
    safe_basename = safe_basename[:48]
    digest = hashlib.sha256(
        unicodedata.normalize("NFC", task_file.relative_path).encode("utf-8")
    ).hexdigest()[:16]
    return f"{safe_basename}-{digest}"
