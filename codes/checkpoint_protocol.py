from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


CHECKPOINT_VERSION = 1
CHECKPOINT_MAX_BYTES = 64 * 1024
CHECKPOINT_DIRECTORY = "checkpoints"
CHECKPOINT_STATUSES = frozenset({"running", "completed", "failed"})
CHECKPOINT_STAGES = frozenset(
    {
        "mineru_parse",
        "mineru_skipped",
        "planning",
        "extract_config",
        "analyzing",
        "coding",
        "evaluation",
        "repair",
        "completed",
    }
)
_CHECKPOINT_KEYS = frozenset(
    {
        "version",
        "job_id",
        "stage_name",
        "stage_sequence",
        "stage_attempt",
        "status",
        "started_at",
        "completed_at",
        "resume_from_stage",
        "error_code",
        "artifacts",
    }
)
_ARTIFACT_KEYS = frozenset({"path", "size", "sha256"})
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CHECKPOINT_NAME_RE = re.compile(
    r"^checkpoint-(?P<sequence>[0-9]{4,8})-"
    r"(?P<stage>[a-z_]+)-(?P<attempt>[0-9]{3,6})\.json$"
)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class CheckpointProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RecoveryCheckpoint:
    checkpoint: dict[str, object]
    checkpoint_path: str
    resume_from_stage: str
    resume_sequence: int
    resume_attempt: int


def _protocol_error(code: str, message: str) -> CheckpointProtocolError:
    return CheckpointProtocolError(code, message)


def _valid_job_id(value: object) -> str:
    if not isinstance(value, str) or not _JOB_ID_RE.fullmatch(value):
        raise _protocol_error("checkpoint_schema_invalid", "Checkpoint job_id is invalid.")
    return value


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _protocol_error(
            "checkpoint_schema_invalid", f"Checkpoint {field} must be a positive integer."
        )
    return value


def _bounded_text(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not value.isprintable()
    ):
        raise _protocol_error(
            "checkpoint_schema_invalid", f"Checkpoint {field} is invalid."
        )
    return value


def _expected_stage_matches(stage_name: str, stage_sequence: int) -> bool:
    if stage_sequence == 1:
        return stage_name in {"mineru_parse", "mineru_skipped"}
    fixed = {
        2: "planning",
        3: "extract_config",
        4: "analyzing",
        5: "coding",
    }
    if stage_sequence in fixed:
        return stage_name == fixed[stage_sequence]
    if stage_sequence < 6:
        return False
    if stage_name == "completed":
        return stage_sequence >= 7 and stage_sequence % 2 == 1
    if stage_sequence % 2 == 0:
        return stage_name == "evaluation"
    return stage_name == "repair"


def checkpoint_stage_order_is_valid(stage_name: str, stage_sequence: int) -> bool:
    return (
        isinstance(stage_name, str)
        and stage_name in CHECKPOINT_STAGES
        and isinstance(stage_sequence, int)
        and not isinstance(stage_sequence, bool)
        and _expected_stage_matches(stage_name, stage_sequence)
    )


def stage_sequence_for_resume(stage_name: str, previous_sequence: int) -> int:
    if stage_name not in CHECKPOINT_STAGES:
        raise _protocol_error("checkpoint_stage_invalid", "Resume stage is not allowed.")
    fixed = {
        "mineru_parse": 1,
        "mineru_skipped": 1,
        "planning": 2,
        "extract_config": 3,
        "analyzing": 4,
        "coding": 5,
    }
    if stage_name in fixed:
        sequence = fixed[stage_name]
    elif stage_name in {"evaluation", "repair", "completed"}:
        sequence = previous_sequence + 1
    else:  # pragma: no cover - the allowlist above keeps this unreachable.
        raise _protocol_error("checkpoint_stage_invalid", "Resume stage is not allowed.")
    if not _expected_stage_matches(stage_name, sequence):
        raise _protocol_error(
            "checkpoint_stage_order_invalid",
            "Resume stage does not follow the completed checkpoint.",
        )
    return sequence


def _validate_resume_transition(
    stage_name: str,
    stage_sequence: int,
    resume_from_stage: str | None,
) -> None:
    allowed = {
        "mineru_parse": frozenset({"planning"}),
        "mineru_skipped": frozenset({"planning"}),
        "planning": frozenset({"extract_config"}),
        "extract_config": frozenset({"analyzing"}),
        "analyzing": frozenset({"coding"}),
        "coding": frozenset({"evaluation"}),
        "evaluation": frozenset({"repair", "completed"}),
        "repair": frozenset({"evaluation"}),
        "completed": frozenset(),
    }
    if resume_from_stage is None:
        if stage_name == "completed" or stage_name == "evaluation":
            return
        raise _protocol_error(
            "checkpoint_stage_order_invalid",
            "Checkpoint resume stage is missing for a resumable boundary.",
        )
    if resume_from_stage not in allowed[stage_name]:
        raise _protocol_error(
            "checkpoint_stage_order_invalid",
            "Checkpoint resume stage does not follow the completed boundary.",
        )
    resume_sequence = stage_sequence_for_resume(resume_from_stage, stage_sequence)
    if resume_sequence != stage_sequence + 1:
        raise _protocol_error(
            "checkpoint_stage_order_invalid",
            "Checkpoint resume stage sequence is inconsistent.",
        )


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise _protocol_error(
            "checkpoint_artifact_path_invalid", "Artifact path is invalid."
        )
    if "\\" in value or ":" in value or value.startswith(("/", "//")):
        raise _protocol_error(
            "checkpoint_artifact_path_invalid", "Artifact path must be relative."
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or len(path.parts) > 16
        or any(
            part in {"", ".", ".."}
            or len(part) > 128
            or unicodedata.normalize("NFC", part) != part
            for part in path.parts
        )
    ):
        raise _protocol_error(
            "checkpoint_artifact_path_invalid", "Artifact path must be normalized."
        )
    for part in path.parts:
        if part.endswith((" ", ".")) or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise _protocol_error(
                "checkpoint_artifact_path_invalid", "Artifact path is unsafe on Windows."
            )
        if any(ord(character) < 32 for character in part):
            raise _protocol_error(
                "checkpoint_artifact_path_invalid", "Artifact path contains control characters."
            )
    return path.as_posix()


def _reparse_or_link(path_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(
        stat.S_ISLNK(path_stat.st_mode)
        or getattr(path_stat, "st_file_attributes", 0) & reparse_flag
    )


def _safe_existing_file(run_dir: Path, relative_path: str) -> Path:
    relative_path = _validate_relative_path(relative_path)
    run_root = Path(os.path.abspath(os.fspath(run_dir)))
    candidate = run_root.joinpath(*PurePosixPath(relative_path).parts)
    current = run_root
    try:
        root_stat = current.lstat()
    except FileNotFoundError as exc:
        raise _protocol_error(
            "checkpoint_artifact_missing", "Checkpoint run directory is missing."
        ) from exc
    if _reparse_or_link(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise _protocol_error(
            "checkpoint_artifact_unsafe", "Checkpoint run directory is unsafe."
        )
    for component in PurePosixPath(relative_path).parts:
        current = current / component
        try:
            current_stat = current.lstat()
        except FileNotFoundError as exc:
            raise _protocol_error(
                "checkpoint_artifact_missing", "Checkpoint artifact is missing."
            ) from exc
        if _reparse_or_link(current_stat):
            raise _protocol_error(
                "checkpoint_artifact_unsafe", "Checkpoint artifact crosses a link."
            )
    target_stat = candidate.lstat()
    if not stat.S_ISREG(target_stat.st_mode) or target_stat.st_nlink != 1:
        raise _protocol_error(
            "checkpoint_artifact_unsafe", "Checkpoint artifact is not a safe regular file."
        )
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_artifacts(
    run_dir: Path,
    artifact_paths: Iterable[str],
) -> list[dict[str, object]]:
    fingerprints: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_path in artifact_paths:
        relative_path = _validate_relative_path(raw_path)
        folded = relative_path.casefold()
        if folded in seen:
            raise _protocol_error(
                "checkpoint_schema_invalid", "Checkpoint artifact paths must be unique."
            )
        seen.add(folded)
        path = _safe_existing_file(run_dir, relative_path)
        size = path.stat().st_size
        fingerprints.append(
            {"path": relative_path, "size": size, "sha256": _sha256_file(path)}
        )
    return fingerprints


def _validate_artifact_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _ARTIFACT_KEYS:
        raise _protocol_error(
            "checkpoint_schema_invalid", "Checkpoint artifact schema is invalid."
        )
    relative_path = _validate_relative_path(value.get("path"))
    size = value.get("size")
    digest = value.get("sha256")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise _protocol_error(
            "checkpoint_schema_invalid", "Checkpoint artifact size is invalid."
        )
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise _protocol_error(
            "checkpoint_schema_invalid", "Checkpoint artifact digest is invalid."
        )
    return {"path": relative_path, "size": size, "sha256": digest}


def validate_checkpoint_mapping(
    value: object,
    *,
    run_dir: Path | None = None,
    expected_job_id: str | None = None,
    validate_artifacts: bool = True,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _protocol_error("checkpoint_schema_invalid", "Checkpoint schema is invalid.")
    if value.get("version") != CHECKPOINT_VERSION:
        raise _protocol_error(
            "checkpoint_unsupported_version", "Checkpoint version is unsupported."
        )
    if set(value) != _CHECKPOINT_KEYS:
        raise _protocol_error("checkpoint_schema_invalid", "Checkpoint schema is invalid.")
    job_id = _valid_job_id(value.get("job_id"))
    if expected_job_id is not None and job_id != expected_job_id:
        raise _protocol_error(
            "checkpoint_job_mismatch", "Checkpoint belongs to a different job."
        )
    stage_name = value.get("stage_name")
    if not isinstance(stage_name, str) or stage_name not in CHECKPOINT_STAGES:
        raise _protocol_error("checkpoint_stage_invalid", "Checkpoint stage is not allowed.")
    stage_sequence = _positive_int(value.get("stage_sequence"), "stage_sequence")
    stage_attempt = _positive_int(value.get("stage_attempt"), "stage_attempt")
    if not _expected_stage_matches(stage_name, stage_sequence):
        raise _protocol_error(
            "checkpoint_stage_order_invalid", "Checkpoint stage order is invalid."
        )
    status_value = value.get("status")
    if not isinstance(status_value, str) or status_value not in CHECKPOINT_STATUSES:
        raise _protocol_error("checkpoint_schema_invalid", "Checkpoint status is invalid.")
    started_at = _bounded_text(value.get("started_at"), "started_at")
    completed_at = _bounded_text(value.get("completed_at"), "completed_at", nullable=True)
    resume_from_stage = value.get("resume_from_stage")
    if resume_from_stage is not None and (
        not isinstance(resume_from_stage, str)
        or resume_from_stage not in CHECKPOINT_STAGES
    ):
        raise _protocol_error(
            "checkpoint_stage_invalid", "Checkpoint resume stage is not allowed."
        )
    error_code = _bounded_text(value.get("error_code"), "error_code", nullable=True)
    artifacts_value = value.get("artifacts")
    if not isinstance(artifacts_value, list) or len(artifacts_value) > 512:
        raise _protocol_error(
            "checkpoint_schema_invalid", "Checkpoint artifacts must be a bounded list."
        )
    artifacts = [_validate_artifact_mapping(item) for item in artifacts_value]
    if len({str(item["path"]).casefold() for item in artifacts}) != len(artifacts):
        raise _protocol_error(
            "checkpoint_schema_invalid", "Checkpoint artifact paths must be unique."
        )
    if status_value == "completed":
        if completed_at is None or not artifacts:
            raise _protocol_error(
                "checkpoint_schema_invalid", "Completed checkpoints require artifacts."
            )
        _validate_resume_transition(stage_name, stage_sequence, resume_from_stage)
        if error_code is not None:
            raise _protocol_error(
                "checkpoint_schema_invalid", "Completed checkpoints cannot contain errors."
            )
    elif status_value == "running":
        if completed_at is not None or resume_from_stage is not None or error_code is not None or artifacts:
            raise _protocol_error(
                "checkpoint_schema_invalid", "Running checkpoint fields are inconsistent."
            )
    else:
        if completed_at is None or error_code is None or resume_from_stage is not None or artifacts:
            raise _protocol_error(
                "checkpoint_schema_invalid", "Failed checkpoint fields are inconsistent."
            )
    normalized = {
        "version": CHECKPOINT_VERSION,
        "job_id": job_id,
        "stage_name": stage_name,
        "stage_sequence": stage_sequence,
        "stage_attempt": stage_attempt,
        "status": status_value,
        "started_at": started_at,
        "completed_at": completed_at,
        "resume_from_stage": resume_from_stage,
        "error_code": error_code,
        "artifacts": artifacts,
    }
    if validate_artifacts and status_value == "completed":
        if run_dir is None:
            raise ValueError("run_dir is required when validating completed artifacts.")
        for artifact in artifacts:
            path = _safe_existing_file(run_dir, str(artifact["path"]))
            if path.stat().st_size != artifact["size"] or _sha256_file(path) != artifact["sha256"]:
                raise _protocol_error(
                    "checkpoint_artifact_mismatch",
                    "Checkpoint artifact fingerprint does not match.",
                )
    return normalized


def running_checkpoint(
    *,
    job_id: str,
    stage_name: str,
    stage_sequence: int,
    stage_attempt: int,
    started_at: str,
) -> dict[str, object]:
    value = {
        "version": CHECKPOINT_VERSION,
        "job_id": job_id,
        "stage_name": stage_name,
        "stage_sequence": stage_sequence,
        "stage_attempt": stage_attempt,
        "status": "running",
        "started_at": started_at,
        "completed_at": None,
        "resume_from_stage": None,
        "error_code": None,
        "artifacts": [],
    }
    return validate_checkpoint_mapping(value, validate_artifacts=False)


def completed_checkpoint(
    run_dir: Path,
    *,
    job_id: str,
    stage_name: str,
    stage_sequence: int,
    stage_attempt: int,
    started_at: str,
    completed_at: str,
    artifact_paths: Iterable[str],
    resume_from_stage: str | None,
) -> dict[str, object]:
    value = {
        "version": CHECKPOINT_VERSION,
        "job_id": job_id,
        "stage_name": stage_name,
        "stage_sequence": stage_sequence,
        "stage_attempt": stage_attempt,
        "status": "completed",
        "started_at": started_at,
        "completed_at": completed_at,
        "resume_from_stage": resume_from_stage,
        "error_code": None,
        "artifacts": fingerprint_artifacts(run_dir, artifact_paths),
    }
    return validate_checkpoint_mapping(value, run_dir=run_dir)


def failed_checkpoint(
    *,
    job_id: str,
    stage_name: str,
    stage_sequence: int,
    stage_attempt: int,
    started_at: str,
    completed_at: str,
    error_code: str,
) -> dict[str, object]:
    value = {
        "version": CHECKPOINT_VERSION,
        "job_id": job_id,
        "stage_name": stage_name,
        "stage_sequence": stage_sequence,
        "stage_attempt": stage_attempt,
        "status": "failed",
        "started_at": started_at,
        "completed_at": completed_at,
        "resume_from_stage": None,
        "error_code": error_code,
        "artifacts": [],
    }
    return validate_checkpoint_mapping(value, validate_artifacts=False)


def checkpoint_relative_path(checkpoint: Mapping[str, object]) -> str:
    sequence = _positive_int(checkpoint.get("stage_sequence"), "stage_sequence")
    attempt = _positive_int(checkpoint.get("stage_attempt"), "stage_attempt")
    stage = checkpoint.get("stage_name")
    if not isinstance(stage, str) or stage not in CHECKPOINT_STAGES:
        raise _protocol_error("checkpoint_stage_invalid", "Checkpoint stage is not allowed.")
    return (
        f"{CHECKPOINT_DIRECTORY}/checkpoint-{sequence:04d}-{stage}-{attempt:03d}.json"
    )


def write_checkpoint(run_dir: Path, checkpoint: Mapping[str, object]) -> Path:
    normalized = validate_checkpoint_mapping(
        dict(checkpoint), run_dir=run_dir, validate_artifacts=True
    )
    relative_path = checkpoint_relative_path(normalized)
    run_root = Path(run_dir)
    run_root.mkdir(parents=True, exist_ok=True)
    run_stat = run_root.lstat()
    if _reparse_or_link(run_stat) or not stat.S_ISDIR(run_stat.st_mode):
        raise _protocol_error("checkpoint_path_unsafe", "Checkpoint run directory is unsafe.")
    checkpoint_dir = run_root / CHECKPOINT_DIRECTORY
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir_stat = checkpoint_dir.lstat()
    if _reparse_or_link(checkpoint_dir_stat) or not stat.S_ISDIR(checkpoint_dir_stat.st_mode):
        raise _protocol_error(
            "checkpoint_path_unsafe", "Checkpoint directory is unsafe."
        )
    target = run_root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        target_stat = None
    if target_stat is not None:
        if _reparse_or_link(target_stat) or not stat.S_ISREG(target_stat.st_mode) or target_stat.st_nlink != 1:
            raise _protocol_error("checkpoint_path_unsafe", "Checkpoint path is unsafe.")
    payload = json.dumps(
        normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > CHECKPOINT_MAX_BYTES:
        raise _protocol_error("checkpoint_too_large", "Checkpoint exceeds the size limit.")
    temp = checkpoint_dir / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def read_checkpoint(
    path: Path,
    *,
    run_dir: Path,
    expected_job_id: str | None = None,
    validate_artifacts: bool = True,
) -> dict[str, object]:
    path = Path(path)
    try:
        relative_path = path.relative_to(Path(run_dir)).as_posix()
    except ValueError as exc:
        raise _protocol_error(
            "checkpoint_path_unsafe", "Checkpoint path is outside the run directory."
        ) from exc
    safe_path = _safe_existing_file(run_dir, relative_path)
    with open(safe_path, "rb") as stream:
        payload = stream.read(CHECKPOINT_MAX_BYTES + 1)
    if len(payload) > CHECKPOINT_MAX_BYTES:
        raise _protocol_error("checkpoint_too_large", "Checkpoint exceeds the size limit.")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _protocol_error(
            "checkpoint_invalid_utf8", "Checkpoint must contain UTF-8 JSON."
        ) from exc
    def reject_duplicate_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise _protocol_error(
                    "checkpoint_schema_invalid", "Checkpoint JSON contains duplicate keys."
                )
            result[key] = item
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                _protocol_error(
                    "checkpoint_invalid_json",
                    f"Checkpoint JSON constant {value} is invalid.",
                )
            ),
        )
    except json.JSONDecodeError as exc:
        raise _protocol_error("checkpoint_invalid_json", "Checkpoint JSON is invalid.") from exc
    normalized = validate_checkpoint_mapping(
        value,
        run_dir=run_dir,
        expected_job_id=expected_job_id,
        validate_artifacts=validate_artifacts,
    )
    match = _CHECKPOINT_NAME_RE.fullmatch(path.name)
    if match is None or (
        int(match.group("sequence")) != normalized["stage_sequence"]
        or match.group("stage") != normalized["stage_name"]
        or int(match.group("attempt")) != normalized["stage_attempt"]
    ):
        raise _protocol_error(
            "checkpoint_path_mismatch", "Checkpoint file name does not match its content."
        )
    return normalized


def _chain_error(message: str) -> CheckpointProtocolError:
    return _protocol_error("checkpoint_stage_order_invalid", message)


def _validate_checkpoint_chain(
    entries: list[tuple[dict[str, object], str]],
) -> None:
    if not entries:
        return
    by_sequence: dict[int, list[dict[str, object]]] = {}
    for checkpoint, _ in entries:
        by_sequence.setdefault(int(checkpoint["stage_sequence"]), []).append(checkpoint)
    sequences = sorted(by_sequence)
    if sequences != list(range(1, sequences[-1] + 1)):
        raise _chain_error("Checkpoint chain contains a sequence gap.")

    completed_by_sequence: dict[int, dict[str, object]] = {}
    for sequence in sequences:
        checkpoints = sorted(
            by_sequence[sequence], key=lambda item: int(item["stage_attempt"])
        )
        stages = {str(item["stage_name"]) for item in checkpoints}
        if len(stages) != 1:
            raise _chain_error("Checkpoint sequence contains conflicting stages.")
        attempts = [int(item["stage_attempt"]) for item in checkpoints]
        if attempts != list(range(1, attempts[-1] + 1)):
            raise _chain_error("Checkpoint attempts are not contiguous and monotonic.")
        # An abrupt process exit can leave its immutable checkpoint at
        # ``running``.  A later contiguous attempt supersedes that stale
        # observation; database lease/process fencing is what prevents the
        # attempts from actually running concurrently.
        completed = [item for item in checkpoints if item["status"] == "completed"]
        if len(completed) > 1:
            raise _chain_error("Checkpoint sequence has conflicting completed attempts.")
        if completed:
            if completed[0] is not checkpoints[-1]:
                raise _chain_error("Checkpoint attempts continue after completion.")
            completed_by_sequence[sequence] = completed[0]
        if sequence < sequences[-1] and sequence not in completed_by_sequence:
            raise _chain_error("A later stage skips an incomplete checkpoint boundary.")

    if len({str(item["stage_name"]) for item in by_sequence[1]}) != 1:
        raise _chain_error("Stage one must use exactly one input branch.")
    for sequence in sequences[:-1]:
        boundary = completed_by_sequence[sequence]
        next_stage = str(by_sequence[sequence + 1][0]["stage_name"])
        if boundary.get("resume_from_stage") != next_stage:
            raise _chain_error("Checkpoint resume stage does not match the next sequence.")
    for sequence, checkpoints in by_sequence.items():
        if str(checkpoints[0]["stage_name"]) == "completed" and sequence != sequences[-1]:
            raise _chain_error("Checkpoint chain continues after the terminal boundary.")


def _closure_error(message: str) -> CheckpointProtocolError:
    return _protocol_error("checkpoint_state_closure_invalid", message)


def _artifact_map(checkpoint: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(artifact["path"]): dict(artifact)
        for artifact in checkpoint["artifacts"]  # type: ignore[index]
    }


def _require_artifact_paths(
    artifacts: Mapping[str, dict[str, object]],
    paths: Iterable[str],
) -> None:
    if any(path not in artifacts for path in paths):
        raise _closure_error("Checkpoint recovery state closure is incomplete.")


def _require_artifact_prefix(
    artifacts: Mapping[str, dict[str, object]],
    prefix: str,
) -> list[str]:
    paths = sorted(path for path in artifacts if path.startswith(prefix))
    if not paths:
        raise _closure_error("Checkpoint recovery state closure is incomplete.")
    return paths


def _require_same_fingerprints(
    current: Mapping[str, dict[str, object]],
    trusted: Mapping[str, dict[str, object]],
    paths: Iterable[str],
) -> None:
    for path in paths:
        if path not in current or path not in trusted or current[path] != trusted[path]:
            raise _closure_error("Checkpoint immutable recovery dependency changed.")


def _completed_boundary(
    entries: list[tuple[dict[str, object], str]],
    sequence: int,
) -> dict[str, object]:
    matches = [
        checkpoint
        for checkpoint, _ in entries
        if int(checkpoint["stage_sequence"]) == sequence
        and checkpoint["status"] == "completed"
    ]
    if len(matches) != 1:
        raise _closure_error("Checkpoint recovery prerequisite boundary is missing.")
    return matches[0]


def _validate_recovery_state_closure(
    run_dir: Path,
    checkpoint: dict[str, object],
    entries: list[tuple[dict[str, object], str]],
) -> None:
    stage_sequence = int(checkpoint["stage_sequence"])
    stage_name = str(checkpoint["stage_name"])
    current = _artifact_map(checkpoint)

    input_boundary = _completed_boundary(entries, 1)
    input_artifacts = _artifact_map(input_boundary)
    markdown_paths = [
        path
        for path in input_artifacts
        if path.lower().endswith((".md", ".markdown"))
    ]
    if len(markdown_paths) != 1:
        raise _closure_error("Input boundary must bind exactly one Markdown file.")
    if stage_sequence == 1:
        _require_same_fingerprints(current, input_artifacts, markdown_paths)
        return

    planning_boundary = _completed_boundary(entries, 2)
    planning_artifacts = _artifact_map(planning_boundary)
    planning_required = [
        "output/task_manifest.json",
        "output/planning_response.json",
        "output/planning_trajectories.json",
    ]
    _require_artifact_paths(planning_artifacts, planning_required)
    _require_same_fingerprints(
        planning_artifacts, input_artifacts, markdown_paths
    )
    immutable_paths = [*markdown_paths, *planning_required]
    _require_same_fingerprints(current, planning_artifacts, immutable_paths)
    if stage_sequence == 2:
        return

    config_boundary = _completed_boundary(entries, 3)
    config_artifacts = _artifact_map(config_boundary)
    config_required = ["output/planning_config.yaml"]
    planning_detail_paths = _require_artifact_prefix(
        config_artifacts, "output/planning_artifacts/"
    )
    _require_same_fingerprints(config_artifacts, planning_artifacts, immutable_paths)
    immutable_paths.extend([*config_required, *planning_detail_paths])
    _require_artifact_paths(config_artifacts, config_required)
    _require_same_fingerprints(current, config_artifacts, immutable_paths)
    if stage_sequence == 3:
        return

    analysis_boundary = _completed_boundary(entries, 4)
    analysis_artifacts = _artifact_map(analysis_boundary)
    analysis_detail_paths = _require_artifact_prefix(
        analysis_artifacts, "output/analyzing_artifacts/"
    )
    analysis_response_paths = sorted(
        path
        for path in analysis_artifacts
        if path.startswith("output/")
        and path.endswith("_simple_analysis_response.json")
    )
    if not analysis_response_paths:
        raise _closure_error("Analyzing boundary is missing required results.")
    _require_same_fingerprints(analysis_artifacts, config_artifacts, immutable_paths)
    immutable_paths.extend([*analysis_detail_paths, *analysis_response_paths])
    _require_same_fingerprints(current, analysis_artifacts, immutable_paths)
    if stage_sequence == 4:
        return

    try:
        try:
            from .task_manifest import load_task_manifest
        except ImportError:  # Script entrypoint imports this module without a package.
            from task_manifest import load_task_manifest
        manifest = load_task_manifest(Path(run_dir) / "output")
    except (OSError, UnicodeError, ValueError) as exc:
        raise _closure_error("TaskManifest recovery dependency is invalid.") from exc
    repo_paths = [f"repo/{task_file.relative_path}" for task_file in manifest.files]
    _require_artifact_paths(current, repo_paths)
    if "output/planning_config.yaml" in current:
        _require_artifact_paths(current, ["repo/config.yaml"])
    if stage_name in {"evaluation", "repair", "completed"}:
        _require_artifact_paths(
            current,
            ["output/repo_status.json", "output/eval_feedback.json"],
        )
    if stage_name == "completed":
        _require_artifact_paths(current, ["run_status.json", "run_summary.json"])


def list_checkpoints(
    run_dir: Path,
    *,
    expected_job_id: str,
) -> list[tuple[dict[str, object], str]]:
    checkpoint_dir = Path(run_dir) / CHECKPOINT_DIRECTORY
    if not checkpoint_dir.exists():
        return []
    directory_stat = checkpoint_dir.lstat()
    if _reparse_or_link(directory_stat) or not stat.S_ISDIR(directory_stat.st_mode):
        raise _protocol_error("checkpoint_path_unsafe", "Checkpoint directory is unsafe.")
    entries: list[tuple[dict[str, object], str]] = []
    for path in sorted(checkpoint_dir.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            continue
        if not path.name.endswith(".json"):
            raise _protocol_error(
                "checkpoint_path_unsafe", "Checkpoint directory contains an unexpected file."
            )
        checkpoint = read_checkpoint(
            path,
            run_dir=run_dir,
            expected_job_id=expected_job_id,
            validate_artifacts=False,
        )
        entries.append((checkpoint, path.relative_to(run_dir).as_posix()))
    entries.sort(
        key=lambda item: (
            int(item[0]["stage_sequence"]),
            int(item[0]["stage_attempt"]),
        )
    )
    seen_attempts: set[tuple[int, int]] = set()
    last_sequence = 0
    for checkpoint, _ in entries:
        sequence = int(checkpoint["stage_sequence"])
        attempt = int(checkpoint["stage_attempt"])
        key = (sequence, attempt)
        if key in seen_attempts or sequence < last_sequence:
            raise _protocol_error(
                "checkpoint_stage_order_invalid", "Checkpoint sequence is not monotonic."
            )
        seen_attempts.add(key)
        last_sequence = sequence
    _validate_checkpoint_chain(entries)
    return entries


def find_recovery_checkpoint(
    run_dir: Path,
    *,
    expected_job_id: str,
) -> RecoveryCheckpoint | None:
    entries = list_checkpoints(run_dir, expected_job_id=expected_job_id)
    latest = latest_completed_checkpoint(
        run_dir,
        expected_job_id=expected_job_id,
        entries=entries,
    )
    if latest is None:
        return None
    checkpoint, relative_path = latest
    resume_from_stage = checkpoint.get("resume_from_stage")
    if not isinstance(resume_from_stage, str):
        return None
    resume_sequence = stage_sequence_for_resume(
        resume_from_stage, int(checkpoint["stage_sequence"])
    )
    attempts = [
        int(item[0]["stage_attempt"])
        for item in entries
        if int(item[0]["stage_sequence"]) == resume_sequence
        and item[0]["stage_name"] == resume_from_stage
    ]
    resume_attempt = max(attempts, default=0) + 1
    return RecoveryCheckpoint(
        checkpoint=checkpoint,
        checkpoint_path=relative_path,
        resume_from_stage=resume_from_stage,
        resume_sequence=resume_sequence,
        resume_attempt=resume_attempt,
    )


def latest_completed_checkpoint(
    run_dir: Path,
    *,
    expected_job_id: str,
    entries: list[tuple[dict[str, object], str]] | None = None,
) -> tuple[dict[str, object], str] | None:
    entries = entries if entries is not None else list_checkpoints(
        run_dir, expected_job_id=expected_job_id
    )
    completed = [item for item in entries if item[0]["status"] == "completed"]
    if not completed:
        return None
    _, relative_path = max(
        completed,
        key=lambda item: (
            int(item[0]["stage_sequence"]),
            int(item[0]["stage_attempt"]),
        ),
    )
    checkpoint_path = Path(run_dir).joinpath(*PurePosixPath(relative_path).parts)
    checkpoint = read_checkpoint(
        checkpoint_path,
        run_dir=run_dir,
        expected_job_id=expected_job_id,
        validate_artifacts=True,
    )
    _validate_recovery_state_closure(run_dir, checkpoint, entries)
    return checkpoint, relative_path
