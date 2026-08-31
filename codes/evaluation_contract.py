"""Versioned evaluation result and repair decision contract.

This module deliberately stores only compact, structured facts. It does not
persist prompts, full provider responses, credentials, or raw transport errors.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from provider_registry import ProviderContractError
    from task_manifest import TaskManifest, safe_join, validate_repair_paths
except ModuleNotFoundError:
    from codes.provider_registry import ProviderContractError
    from codes.task_manifest import TaskManifest, safe_join, validate_repair_paths


EVALUATION_RESULT_SCHEMA_VERSION = 1
PASS_RULE = "score >= 4.0, quorum met, and no high severity findings"
LEGACY_STATUS_PENDING_EVAL = "待测评"
LEGACY_STATUS_EVAL_FAILED = "测评但未通过"
LEGACY_STATUS_EVAL_PASSED = "测评且通过"
LEGACY_STATUS_EVAL_ERROR = "测评协议失败"

EXECUTION_STATUSES = frozenset({"queued", "running", "completed", "failed", "canceled"})
EVALUATION_STATUSES = frozenset({"pending", "running", "completed", "failed", "skipped"})
QUALITY_STATUSES = frozenset({"pending", "assessing", "accepted", "rejected", "skipped"})
QUALITY_VERDICTS = frozenset({"passed", "failed", "not_assessed"})
REPAIR_STATUSES = frozenset({"not_applicable", "pending", "skipped", "blocked"})
EVALUATION_ERROR_CODES = frozenset(
    {
        "execution_failed",
        "evaluator_timeout",
        "provider_protocol_error",
        "evaluator_unavailable",
        "malformed_evaluator_response",
        "quorum_not_met",
    }
)

_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "paper_name",
        "target_repo_dir",
        "eval_type",
        "provider_id",
        "requested_eval_model",
        "eval_model",
        "generated_n",
        "valid_n",
        "execution_status",
        "evaluation_status",
        "quality_status",
        "quality_verdict",
        "quality_score",
        "score_lst",
        "has_high_severity",
        "pass_rule",
        "findings",
        "findings_by_file",
        "files_to_fix",
        "repair_round",
        "max_repair_rounds",
        "repair_status",
        "errors",
        "fallback_used",
        "fallback_reason",
        "fallback_from_model",
        "fallback_eval_model",
        "fallback_model_chain",
        "fallback_remaining_models",
        "completed_repair_attempts",
    }
)
_FORBIDDEN_FIELDS = frozenset(
    {
        "prompt",
        "request_json",
        "completion_json",
        "response",
        "response_body",
        "model_response",
        "raw_response",
        "api_key",
        "authorization",
        "credentials",
    }
)
_SECRET_MARKERS = (
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"gho_[A-Za-z0-9_]+"),
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"authorization", re.IGNORECASE),
)


class EvaluationContractError(ValueError):
    """Stable non-secret contract error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _contract_error(code: str, message: str) -> EvaluationContractError:
    return EvaluationContractError(code, message)


def _require_text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _contract_error("evaluation_schema_invalid", f"{field_name} must be text.")
    if not allow_empty and not value:
        raise _contract_error(
            "evaluation_schema_invalid",
            f"{field_name} must not be empty.",
        )
    return value


def _require_int(value: object, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _contract_error(
            "evaluation_schema_invalid",
            f"{field_name} must be an integer >= {minimum}.",
        )
    return value


def _require_string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _contract_error(
            "evaluation_schema_invalid",
            f"{field_name} must be a list of strings.",
        )
    return list(value)


def _require_mapping_list(value: object, field_name: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise _contract_error(
            "evaluation_schema_invalid",
            f"{field_name} must be a list of objects.",
        )
    return [dict(item) for item in value]


def _require_number_list(value: object, field_name: str) -> list[int | float]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) for item in value
    ):
        raise _contract_error(
            "evaluation_schema_invalid",
            f"{field_name} must be a list of numbers.",
        )
    return list(value)


def _has_sensitive_text(value: object) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_MARKERS)
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in _FORBIDDEN_FIELDS or _has_sensitive_text(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_has_sensitive_text(item) for item in value)
    return False


def _assert_non_sensitive_mapping(value: Mapping[str, object]) -> None:
    forbidden = set(value) & _FORBIDDEN_FIELDS
    if forbidden:
        raise _contract_error(
            "evaluation_schema_forbidden_payload",
            "Evaluation result contains a forbidden raw payload field.",
        )
    if _has_sensitive_text(value):
        raise _contract_error(
            "evaluation_schema_forbidden_secret",
            "Evaluation result contains sensitive text.",
        )


def required_quorum(generated_n: int) -> int:
    generated_n = _require_int(generated_n, "generated_n", minimum=1)
    return generated_n // 2 + 1


def has_quorum(*, valid_n: int, generated_n: int) -> bool:
    valid_n = _require_int(valid_n, "valid_n", minimum=0)
    return valid_n >= required_quorum(generated_n)


def _fallback_defaults(overrides: Mapping[str, object]) -> dict[str, object]:
    return {
        "fallback_used": bool(overrides.get("fallback_used", False)),
        "fallback_reason": str(overrides.get("fallback_reason") or ""),
        "fallback_from_model": str(overrides.get("fallback_from_model") or ""),
        "fallback_eval_model": str(overrides.get("fallback_eval_model") or ""),
        "fallback_model_chain": list(overrides.get("fallback_model_chain") or []),
        "fallback_remaining_models": list(
            overrides.get("fallback_remaining_models") or []
        ),
    }


def _findings_by_file(findings: Sequence[Mapping[str, object]]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for finding in findings:
        file_name = str(finding.get("file_name") or "repository")
        grouped.setdefault(file_name, []).append(dict(finding))
    return grouped


def _repair_status_for_quality_failure(
    *,
    files_to_fix: Sequence[str],
    repair_round: int,
    max_repair_rounds: int,
) -> str:
    if not files_to_fix:
        return "skipped"
    if repair_round >= max_repair_rounds:
        return "blocked"
    return "pending"


def build_quality_result(
    *,
    paper_name: str,
    target_repo_dir: str,
    eval_type: str,
    requested_eval_model: str,
    eval_model: str,
    provider_id: str,
    generated_n: int,
    scores: Sequence[int | float],
    findings: Sequence[Mapping[str, object]],
    files_to_fix: Sequence[str],
    repair_round: int,
    max_repair_rounds: int,
    completed_repair_attempts: Sequence[Mapping[str, object]] | None = None,
    **fallback_info: object,
) -> dict[str, object]:
    generated_n = _require_int(generated_n, "generated_n", minimum=1)
    repair_round = _require_int(repair_round, "repair_round", minimum=0)
    max_repair_rounds = _require_int(
        max_repair_rounds,
        "max_repair_rounds",
        minimum=0,
    )
    normalized_scores = []
    for score in scores:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise _contract_error("evaluation_schema_invalid", "scores must be numeric.")
        normalized_scores.append(float(score))
    valid_n = len(normalized_scores)
    if not has_quorum(valid_n=valid_n, generated_n=generated_n):
        return build_quorum_not_met_result(
            paper_name=paper_name,
            target_repo_dir=target_repo_dir,
            eval_type=eval_type,
            requested_eval_model=requested_eval_model,
            eval_model=eval_model,
            provider_id=provider_id,
            generated_n=generated_n,
            valid_n=valid_n,
            repair_round=repair_round,
            max_repair_rounds=max_repair_rounds,
            completed_repair_attempts=completed_repair_attempts,
            **fallback_info,
        )

    normalized_findings = [dict(finding) for finding in findings]
    has_high = any(
        str(finding.get("severity_level") or "").strip().lower() == "high"
        for finding in normalized_findings
    )
    quality_score = sum(normalized_scores) / valid_n
    passed = quality_score >= 4.0 and not has_high
    quality_status = "accepted" if passed else "rejected"
    quality_verdict = "passed" if passed else "failed"
    normalized_files = list(files_to_fix)
    result = {
        "schema_version": EVALUATION_RESULT_SCHEMA_VERSION,
        "paper_name": _require_text(paper_name, "paper_name"),
        "target_repo_dir": _require_text(target_repo_dir, "target_repo_dir"),
        "eval_type": _require_text(eval_type, "eval_type"),
        "provider_id": _require_text(provider_id, "provider_id"),
        "requested_eval_model": _require_text(
            requested_eval_model,
            "requested_eval_model",
        ),
        "eval_model": _require_text(eval_model, "eval_model"),
        "generated_n": generated_n,
        "valid_n": valid_n,
        "execution_status": "completed",
        "evaluation_status": "completed",
        "quality_status": quality_status,
        "quality_verdict": quality_verdict,
        "quality_score": quality_score,
        "score_lst": [int(score) if score.is_integer() else score for score in normalized_scores],
        "has_high_severity": has_high,
        "pass_rule": PASS_RULE,
        "findings": normalized_findings,
        "findings_by_file": _findings_by_file(normalized_findings),
        "files_to_fix": normalized_files,
        "repair_round": repair_round,
        "max_repair_rounds": max_repair_rounds,
        "repair_status": (
            "not_applicable"
            if passed
            else _repair_status_for_quality_failure(
                files_to_fix=normalized_files,
                repair_round=repair_round,
                max_repair_rounds=max_repair_rounds,
            )
        ),
        "errors": [],
        "completed_repair_attempts": [
            dict(attempt) for attempt in (completed_repair_attempts or [])
        ],
        **_fallback_defaults(fallback_info),
    }
    return validate_evaluation_result(result)


def _build_non_quality_result(
    *,
    paper_name: str,
    target_repo_dir: str,
    eval_type: str,
    provider_id: str,
    requested_eval_model: str,
    eval_model: str,
    generated_n: int,
    valid_n: int,
    execution_status: str,
    evaluation_status: str,
    error_code: str,
    error_message: str,
    repair_round: int,
    max_repair_rounds: int,
    completed_repair_attempts: Sequence[Mapping[str, object]] | None = None,
    **fallback_info: object,
) -> dict[str, object]:
    error_code = _require_text(error_code, "error_code")
    if error_code not in EVALUATION_ERROR_CODES:
        raise _contract_error("evaluation_error_code_invalid", "Unknown error code.")
    if _has_sensitive_text({"message": error_message}):
        raise _contract_error(
            "evaluation_schema_forbidden_secret",
            "Evaluation error message contains sensitive text.",
        )
    result = {
        "schema_version": EVALUATION_RESULT_SCHEMA_VERSION,
        "paper_name": _require_text(paper_name, "paper_name"),
        "target_repo_dir": _require_text(target_repo_dir, "target_repo_dir"),
        "eval_type": _require_text(eval_type, "eval_type"),
        "provider_id": str(provider_id or ""),
        "requested_eval_model": str(requested_eval_model or ""),
        "eval_model": str(eval_model or ""),
        "generated_n": _require_int(generated_n, "generated_n", minimum=1),
        "valid_n": _require_int(valid_n, "valid_n", minimum=0),
        "execution_status": execution_status,
        "evaluation_status": evaluation_status,
        "quality_status": "skipped",
        "quality_verdict": "not_assessed",
        "quality_score": None,
        "score_lst": [],
        "has_high_severity": False,
        "pass_rule": PASS_RULE,
        "findings": [],
        "findings_by_file": {},
        "files_to_fix": [],
        "repair_round": _require_int(repair_round, "repair_round", minimum=0),
        "max_repair_rounds": _require_int(
            max_repair_rounds,
            "max_repair_rounds",
            minimum=0,
        ),
        "repair_status": "blocked",
        "errors": [{"code": error_code, "message": error_message}],
        "completed_repair_attempts": [
            dict(attempt) for attempt in (completed_repair_attempts or [])
        ],
        **_fallback_defaults(fallback_info),
    }
    return validate_evaluation_result(result)


def build_evaluation_error_result(
    *,
    paper_name: str,
    target_repo_dir: str,
    eval_type: str,
    requested_eval_model: str,
    provider_id: str,
    error_code: str,
    error_message: str,
    generated_n: int,
    repair_round: int,
    max_repair_rounds: int,
    eval_model: str = "",
    **fallback_info: object,
) -> dict[str, object]:
    return _build_non_quality_result(
        paper_name=paper_name,
        target_repo_dir=target_repo_dir,
        eval_type=eval_type,
        provider_id=provider_id,
        requested_eval_model=requested_eval_model,
        eval_model=eval_model,
        generated_n=generated_n,
        valid_n=0,
        execution_status="completed",
        evaluation_status="failed",
        error_code=error_code,
        error_message=error_message,
        repair_round=repair_round,
        max_repair_rounds=max_repair_rounds,
        **fallback_info,
    )


def build_quorum_not_met_result(
    *,
    paper_name: str,
    target_repo_dir: str,
    eval_type: str,
    requested_eval_model: str,
    eval_model: str,
    provider_id: str,
    generated_n: int,
    valid_n: int,
    repair_round: int,
    max_repair_rounds: int,
    **fallback_info: object,
) -> dict[str, object]:
    return _build_non_quality_result(
        paper_name=paper_name,
        target_repo_dir=target_repo_dir,
        eval_type=eval_type,
        provider_id=provider_id,
        requested_eval_model=requested_eval_model,
        eval_model=eval_model,
        generated_n=generated_n,
        valid_n=valid_n,
        execution_status="completed",
        evaluation_status="failed",
        error_code="quorum_not_met",
        error_message="Evaluator responses did not meet quorum.",
        repair_round=repair_round,
        max_repair_rounds=max_repair_rounds,
        **fallback_info,
    )


def build_execution_failed_result(
    *,
    paper_name: str,
    target_repo_dir: str,
    eval_type: str,
    failure_code: str,
    repair_round: int,
    max_repair_rounds: int,
) -> dict[str, object]:
    return _build_non_quality_result(
        paper_name=paper_name,
        target_repo_dir=target_repo_dir,
        eval_type=eval_type,
        provider_id="",
        requested_eval_model="",
        eval_model="",
        generated_n=1,
        valid_n=0,
        execution_status="failed",
        evaluation_status="skipped",
        error_code="execution_failed",
        error_message=f"Execution failed before evaluation: {failure_code}.",
        repair_round=repair_round,
        max_repair_rounds=max_repair_rounds,
    )


def validate_evaluation_result(payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise _contract_error("evaluation_schema_invalid", "Evaluation result must be an object.")
    _assert_non_sensitive_mapping(payload)
    if set(payload) != _RESULT_FIELDS:
        raise _contract_error(
            "evaluation_schema_invalid",
            "Evaluation result fields do not match schema v1.",
        )
    if payload["schema_version"] != EVALUATION_RESULT_SCHEMA_VERSION:
        raise _contract_error(
            "evaluation_schema_version_unsupported",
            "Unsupported evaluation result schema version.",
        )
    execution_status = _require_text(payload["execution_status"], "execution_status")
    evaluation_status = _require_text(payload["evaluation_status"], "evaluation_status")
    quality_status = _require_text(payload["quality_status"], "quality_status")
    quality_verdict = _require_text(payload["quality_verdict"], "quality_verdict")
    repair_status = _require_text(payload["repair_status"], "repair_status")
    if execution_status not in EXECUTION_STATUSES:
        raise _contract_error("evaluation_schema_invalid", "Invalid execution status.")
    if evaluation_status not in EVALUATION_STATUSES:
        raise _contract_error("evaluation_schema_invalid", "Invalid evaluation status.")
    if quality_status not in QUALITY_STATUSES:
        raise _contract_error("evaluation_schema_invalid", "Invalid quality status.")
    if quality_verdict not in QUALITY_VERDICTS:
        raise _contract_error("evaluation_schema_invalid", "Invalid quality verdict.")
    if repair_status not in REPAIR_STATUSES:
        raise _contract_error("evaluation_schema_invalid", "Invalid repair status.")

    generated_n = _require_int(payload["generated_n"], "generated_n", minimum=1)
    valid_n = _require_int(payload["valid_n"], "valid_n", minimum=0)
    repair_round = _require_int(payload["repair_round"], "repair_round", minimum=0)
    max_repair_rounds = _require_int(
        payload["max_repair_rounds"],
        "max_repair_rounds",
        minimum=0,
    )
    if valid_n > generated_n:
        raise _contract_error("evaluation_schema_invalid", "valid_n exceeds generated_n.")
    if repair_round > max_repair_rounds:
        raise _contract_error(
            "evaluation_schema_invalid",
            "repair_round exceeds max_repair_rounds.",
        )
    score = payload["quality_score"]
    if quality_verdict == "not_assessed":
        if score is not None or quality_status != "skipped":
            raise _contract_error(
                "evaluation_schema_invalid",
                "Unassessed quality must not include a score.",
            )
    elif not isinstance(score, (int, float)) or isinstance(score, bool):
        raise _contract_error(
            "evaluation_schema_invalid",
            "Assessed quality must include a numeric score.",
        )
    if quality_status == "accepted" and quality_verdict != "passed":
        raise _contract_error(
            "evaluation_schema_invalid",
            "Accepted quality must have a passed verdict.",
        )
    if quality_status == "rejected" and quality_verdict != "failed":
        raise _contract_error(
            "evaluation_schema_invalid",
            "Rejected quality must have a failed verdict.",
        )
    if quality_verdict == "passed" and quality_status != "accepted":
        raise _contract_error(
            "evaluation_schema_invalid",
            "Passed quality verdict must be accepted.",
        )
    if quality_verdict == "failed" and quality_status != "rejected":
        raise _contract_error(
            "evaluation_schema_invalid",
            "Failed quality verdict must be rejected.",
        )
    if evaluation_status != "completed" and (
        quality_status in {"accepted", "rejected"}
        or quality_verdict != "not_assessed"
    ):
        raise _contract_error(
            "evaluation_schema_invalid",
            "Only completed evaluations can make a quality conclusion.",
        )
    if evaluation_status == "completed" and quality_verdict == "not_assessed":
        raise _contract_error(
            "evaluation_schema_invalid",
            "Completed evaluations must make a quality conclusion.",
        )
    errors = _require_mapping_list(payload["errors"], "errors")
    if evaluation_status == "failed" and not errors:
        raise _contract_error(
            "evaluation_schema_invalid",
            "Failed evaluation must include a structured error.",
        )
    if evaluation_status == "completed" and errors:
        raise _contract_error(
            "evaluation_schema_invalid",
            "Completed evaluation must not include errors.",
        )
    for error in errors:
        if error.get("code") not in EVALUATION_ERROR_CODES:
            raise _contract_error("evaluation_schema_invalid", "Invalid evaluation error.")
    _require_number_list(payload["score_lst"], "score_lst")
    _require_mapping_list(payload["findings"], "findings")
    if not isinstance(payload["findings_by_file"], Mapping):
        raise _contract_error(
            "evaluation_schema_invalid",
            "findings_by_file must be an object.",
        )
    _require_string_list(payload["files_to_fix"], "files_to_fix")
    _require_mapping_list(payload["completed_repair_attempts"], "completed_repair_attempts")
    _require_string_list(payload["fallback_model_chain"], "fallback_model_chain")
    _require_string_list(
        payload["fallback_remaining_models"],
        "fallback_remaining_models",
    )
    for field_name in (
        "paper_name",
        "target_repo_dir",
        "eval_type",
        "provider_id",
        "requested_eval_model",
        "eval_model",
        "fallback_reason",
        "fallback_from_model",
        "fallback_eval_model",
        "pass_rule",
    ):
        _require_text(payload[field_name], field_name, allow_empty=True)
    if not isinstance(payload["fallback_used"], bool):
        raise _contract_error("evaluation_schema_invalid", "fallback_used must be boolean.")
    if not isinstance(payload["has_high_severity"], bool):
        raise _contract_error(
            "evaluation_schema_invalid",
            "has_high_severity must be boolean.",
        )
    return dict(payload)


def extract_evaluation_result(payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise _contract_error("evaluation_schema_invalid", "Evaluation result must be an object.")
    _assert_non_sensitive_mapping(payload)
    return validate_evaluation_result(
        {field: payload[field] for field in _RESULT_FIELDS if field in payload}
    )


def _legacy_decision(result: Mapping[str, object]) -> dict[str, object]:
    status = result.get("status")
    repair_round = int(result.get("repair_round", 0) or 0)
    max_repair_rounds = int(result.get("max_repair_rounds", 0) or 0)
    files = list(result.get("files_to_fix") or result.get("files_to_repair") or [])
    if status == LEGACY_STATUS_EVAL_PASSED:
        return {
            "status": "skipped",
            "reason": "quality_accepted",
            "attempt": repair_round,
            "max_attempts": max_repair_rounds,
            "files_to_fix": [],
        }
    if status != LEGACY_STATUS_EVAL_FAILED:
        return {
            "status": "blocked",
            "reason": "evaluation_not_repairable",
            "attempt": repair_round,
            "max_attempts": max_repair_rounds,
            "files_to_fix": files,
        }
    if not files:
        return {
            "status": "skipped",
            "reason": "no_files_to_fix",
            "attempt": repair_round,
            "max_attempts": max_repair_rounds,
            "files_to_fix": [],
        }
    if repair_round >= max_repair_rounds:
        return {
            "status": "blocked",
            "reason": "repair_limit_reached",
            "attempt": repair_round,
            "max_attempts": max_repair_rounds,
            "files_to_fix": files,
        }
    return {
        "status": "ready",
        "reason": "quality_rejected",
        "attempt": repair_round + 1,
        "max_attempts": max_repair_rounds,
        "files_to_fix": files,
    }


def decide_repair_action(result: Mapping[str, object]) -> dict[str, object]:
    if result.get("schema_version") != EVALUATION_RESULT_SCHEMA_VERSION:
        return _legacy_decision(result)
    validated = validate_evaluation_result(result)
    repair_round = int(validated["repair_round"])
    max_attempts = int(validated["max_repair_rounds"])
    files = list(validated["files_to_fix"])
    base = {
        "attempt": repair_round,
        "max_attempts": max_attempts,
        "files_to_fix": files,
    }
    if validated["execution_status"] != "completed":
        return {"status": "blocked", "reason": "execution_failed", **base}
    if validated["evaluation_status"] == "failed":
        reason = str(validated["errors"][0]["code"])
        return {"status": "blocked", "reason": reason, **base}
    if validated["quality_status"] == "accepted":
        return {"status": "skipped", "reason": "quality_accepted", **base}
    if validated["quality_status"] != "rejected":
        return {"status": "blocked", "reason": "quality_not_rejected", **base}
    if not files:
        return {"status": "skipped", "reason": "no_files_to_fix", **base}
    if repair_round >= max_attempts:
        return {"status": "blocked", "reason": "repair_limit_reached", **base}
    if validated["repair_status"] != "pending":
        return {"status": "blocked", "reason": "repair_status_not_pending", **base}

    next_attempt = repair_round + 1
    for attempt in validated["completed_repair_attempts"]:
        if (
            attempt.get("attempt") == next_attempt
            and attempt.get("status") == "completed"
            and list(attempt.get("files_to_fix") or []) == files
        ):
            return {
                "status": "skipped",
                "reason": "repair_already_completed",
                "attempt": next_attempt,
                "max_attempts": max_attempts,
                "files_to_fix": files,
            }
    return {
        "status": "ready",
        "reason": "quality_rejected",
        "attempt": next_attempt,
        "max_attempts": max_attempts,
        "files_to_fix": files,
    }


def resolve_files_to_fix(
    raw_paths: object,
    manifest: TaskManifest,
    *,
    repo_root: object | None = None,
) -> tuple[str, ...]:
    selected = validate_repair_paths(raw_paths, manifest)
    if repo_root is not None:
        for task_file in selected:
            safe_join(repo_root, task_file)
    return tuple(task_file.relative_path for task_file in selected)


def legacy_status_from_result(result: Mapping[str, object]) -> str:
    if result.get("schema_version") != EVALUATION_RESULT_SCHEMA_VERSION:
        return str(result.get("status") or "")
    if result["quality_status"] == "accepted":
        return LEGACY_STATUS_EVAL_PASSED
    if result["quality_status"] == "rejected":
        return LEGACY_STATUS_EVAL_FAILED
    if result["evaluation_status"] in {"failed", "skipped"}:
        return LEGACY_STATUS_EVAL_ERROR
    return LEGACY_STATUS_PENDING_EVAL


def classify_evaluation_exception(error: BaseException) -> dict[str, str]:
    code = "evaluator_unavailable"
    error_code = getattr(error, "code", "")
    if isinstance(error, TimeoutError):
        code = "evaluator_timeout"
    elif error_code == "malformed_evaluator_response":
        code = "malformed_evaluator_response"
    elif error.__class__.__name__ in {
        "TaskManifestError",
        "InvalidTaskPathError",
        "UnsafeTaskWriteError",
    }:
        code = "malformed_evaluator_response"
    elif error_code in {
        "provider_response_choice_count_mismatch",
        "provider_response_usage_invalid",
    }:
        code = "provider_protocol_error"
    elif isinstance(error, ProviderContractError):
        code = "provider_protocol_error"
    elif getattr(error, "status_code", None) == 408:
        code = "evaluator_timeout"
    elif getattr(error, "status_code", None) in {400, 422}:
        code = "provider_protocol_error"
    elif error.__class__.__name__.lower().endswith("timeout"):
        code = "evaluator_timeout"

    messages = {
        "evaluator_timeout": "Evaluator request timed out.",
        "provider_protocol_error": "Evaluator provider returned a protocol error.",
        "evaluator_unavailable": "Evaluator provider was unavailable.",
        "malformed_evaluator_response": "Evaluator response was malformed.",
    }
    return {"error_code": code, "message": messages[code]}
