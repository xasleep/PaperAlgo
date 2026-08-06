from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidStateTransitionError


EXECUTION_TRANSITIONS = {
    "queued": frozenset({"running", "failed", "canceled"}),
    "running": frozenset({"completed", "failed", "canceled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "canceled": frozenset(),
}
EVALUATION_TRANSITIONS = {
    "pending": frozenset({"running", "passed", "failed", "skipped"}),
    "running": frozenset({"passed", "failed", "skipped"}),
    "passed": frozenset(),
    "failed": frozenset(),
    "skipped": frozenset(),
}
QUALITY_TRANSITIONS = {
    "pending": frozenset({"assessing", "accepted", "rejected", "skipped"}),
    "assessing": frozenset({"accepted", "rejected", "skipped"}),
    "accepted": frozenset(),
    "rejected": frozenset(),
    "skipped": frozenset(),
}


@dataclass(frozen=True)
class JobState:
    execution_status: str
    evaluation_status: str
    quality_status: str


def _validate_composite_state(state: JobState) -> None:
    execution = state.execution_status
    evaluation = state.evaluation_status
    quality = state.quality_status
    valid = False
    if execution in {"queued", "running"}:
        valid = evaluation == "pending" and quality == "pending"
    elif execution in {"failed", "canceled"}:
        valid = evaluation in {"pending", "skipped"} and quality in {
            "pending",
            "skipped",
        }
    elif execution == "completed":
        if evaluation in {"pending", "running"}:
            valid = quality == "pending"
        elif evaluation in {"passed", "failed", "skipped"}:
            valid = quality in QUALITY_TRANSITIONS

    if not valid:
        raise InvalidStateTransitionError(
            details={
                "reason": "invalid_composite_state",
                "execution_status": execution,
                "evaluation_status": evaluation,
                "quality_status": quality,
            }
        )


def validate_job_transition(
    current: JobState,
    *,
    execution_status: str | None = None,
    evaluation_status: str | None = None,
    quality_status: str | None = None,
) -> JobState:
    requested = {
        "execution_status": execution_status,
        "evaluation_status": evaluation_status,
        "quality_status": quality_status,
    }
    transition_maps = {
        "execution_status": EXECUTION_TRANSITIONS,
        "evaluation_status": EVALUATION_TRANSITIONS,
        "quality_status": QUALITY_TRANSITIONS,
    }
    changed = False
    next_values: dict[str, str] = {}
    for field, target in requested.items():
        source = getattr(current, field)
        if target is None or target == source:
            next_values[field] = source
            continue
        allowed = transition_maps[field].get(source, frozenset())
        if target not in allowed:
            raise InvalidStateTransitionError(
                details={"field": field, "from": source, "to": target}
            )
        next_values[field] = target
        changed = True

    if not changed:
        raise InvalidStateTransitionError(
            "At least one job status must change.",
            details={"reason": "no_status_change"},
        )
    next_state = JobState(**next_values)
    _validate_composite_state(next_state)
    return next_state
