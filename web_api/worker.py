from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from codes.checkpoint_protocol import (
    CheckpointProtocolError,
    find_recovery_checkpoint,
    latest_completed_checkpoint,
    list_checkpoints,
)
from codes.evaluation_contract import (
    EvaluationContractError,
    decide_repair_action,
    extract_evaluation_result,
)
from codes.task_manifest import TaskManifestError, load_task_manifest

from . import job_service
from .config import REPO_ROOT, configured_job_runtime
from .errors import OptimisticLockConflictError, SettingsNotConfiguredError
from .job_repository import JobRepository
from .process_identity import (
    get_process_create_time,
    process_identity_status,
    terminate_verified_process_tree,
)
from .settings_store import load_settings


@dataclass(frozen=True)
class LaunchSpec:
    command: list[str]
    environment: dict[str, str]
    command_summary: str


@dataclass
class ManagedProcess:
    job_id: str
    launch_token: str
    pid: int
    process_create_time: str
    process_group_id: int
    process: subprocess.Popen[str] | None = None
    log_file: TextIO | None = None


@dataclass(frozen=True)
class ReconcileResult:
    safe_to_schedule: bool
    unresolved_job_ids: tuple[str, ...]
    recovering_job_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfirmedExitDecision:
    has_local_process: bool
    observed_exit_code: int | None
    cancel_pending: bool
    identity_confirmed_dead: bool
    recovery_allowed: bool
    recovery_count: int
    max_recoveries: int


def _safe_load_json(path: Path) -> object:
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


CommandBuilder = Callable[[dict[str, object]], LaunchSpec]
FORCE_CANCEL_SECONDS = 5.0
DEFAULT_MAX_RECOVERIES = 1


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _pipeline_launch_spec(job: dict[str, object]) -> LaunchSpec:
    settings = load_settings()
    if settings is None:
        raise SettingsNotConfiguredError()
    job_service.validate_provider_selection_snapshot(job, settings)
    pdf_path = job_service.resolve_upload(str(job["upload_id"]))
    command = job_service.build_pipeline_command(
        pdf_path=pdf_path,
        job_id=str(job["job_id"]),
        paper_name=str(job["paper_name"]),
        settings=settings,
        domain=str(job["domain"]),  # type: ignore[arg-type]
        eval_type=str(job["eval_type"]),  # type: ignore[arg-type]
        generated_n=int(job["generated_n"]),
        auto_refine=bool(job["auto_refine"]),
        max_repair_rounds=int(job["max_repair_rounds"]),
        console_output=str(job["console_output"]),  # type: ignore[arg-type]
        skip_mineru=bool(job.get("_skip_mineru_for_resume", job["skip_mineru"])),
        pdf_markdown_path=str(job["pdf_markdown_path"]),
        checkpoint_mode="sqlite",
        resume_from_stage=str(job.get("_resume_from_stage") or ""),
        resume_stage_sequence=int(job.get("_resume_stage_sequence") or 0),
        resume_stage_attempt=int(job.get("_resume_stage_attempt") or 0),
        checkpoint_recovery_count=int(job.get("_recovery_count") or 0),
    )
    summary = json.dumps(
        {
            "entrypoint": Path(command[1]).name,
            "executable": Path(command[0]).name,
            "job_id": str(job["job_id"]),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return LaunchSpec(
        command=command,
        environment=job_service.build_pipeline_env(settings),
        command_summary=summary,
    )


class PipelineWorker:
    def __init__(
        self,
        *,
        repository: JobRepository | None = None,
        worker_id: str | None = None,
        instance_token: str | None = None,
        max_concurrency: int = 1,
        poll_interval: float = 0.25,
        lease_seconds: float = 30.0,
        cancel_grace_seconds: float = 10.0,
        max_recoveries: int = DEFAULT_MAX_RECOVERIES,
        command_builder: CommandBuilder | None = None,
    ) -> None:
        if max_concurrency != 1:
            raise ValueError("PR-04A supports max_concurrency=1 only.")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive.")
        if lease_seconds <= poll_interval:
            raise ValueError("lease_seconds must be greater than poll_interval.")
        if cancel_grace_seconds < 0 or cancel_grace_seconds > 10:
            raise ValueError("cancel_grace_seconds must be between 0 and 10.")
        if lease_seconds <= cancel_grace_seconds + FORCE_CANCEL_SECONDS:
            raise ValueError(
                "lease_seconds must exceed the graceful and forced cancel windows."
            )
        if (
            not isinstance(max_recoveries, int)
            or isinstance(max_recoveries, bool)
            or max_recoveries < 1
        ):
            raise ValueError("max_recoveries must be a positive integer.")
        self.repository = repository or JobRepository()
        self.worker_id = worker_id or _default_worker_id()
        self.instance_token = instance_token or uuid.uuid4().hex
        self.max_concurrency = max_concurrency
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.cancel_grace_seconds = cancel_grace_seconds
        self.max_recoveries = max_recoveries
        self.command_builder = command_builder or _pipeline_launch_spec
        self._managed: dict[str, ManagedProcess] = {}
        self._unresolved_job_ids: tuple[str, ...] = ()
        self._last_reconcile_result = ReconcileResult(True, (), ())
        self._lease_owned = False
        self._reconciled = False

    @property
    def last_reconcile_result(self) -> ReconcileResult:
        return self._last_reconcile_result

    def _forget(self, job_id: str) -> None:
        managed = self._managed.pop(job_id, None)
        if managed is not None and managed.log_file is not None:
            managed.log_file.close()

    def _detach_all(self) -> None:
        for job_id in list(self._managed):
            self._forget(job_id)

    def _fail_managed(
        self,
        managed: ManagedProcess,
        failure_code: str,
        *,
        event_type: str = "job.process_failed",
        exit_code: int | None = None,
        cancel_command_error_code: str | None = None,
    ) -> None:
        self.repository.fail_process(
            managed.job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
            launch_token=managed.launch_token,
            failure_code=failure_code,
            event_type=event_type,
            exit_code=exit_code,
            cancel_command_error_code=cancel_command_error_code,
        )
        self._forget(managed.job_id)

    def _final_evaluation_result(self, job_id: str) -> dict[str, object]:
        run_dir = job_service.RUNS_DIR / job_id
        latest_completed = latest_completed_checkpoint(run_dir, expected_job_id=job_id)
        if latest_completed is None or latest_completed[0]["stage_name"] != "completed":
            raise CheckpointProtocolError(
                "final_checkpoint_missing",
                "Completed pipeline checkpoint is missing.",
            )
        feedback_path = run_dir / "output" / "eval_feedback.json"
        try:
            feedback = _safe_load_json(feedback_path)
            result = extract_evaluation_result(feedback)  # type: ignore[arg-type]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CheckpointProtocolError(
                "final_evaluation_missing",
                "Final evaluation result is missing or unreadable.",
            ) from exc
        except (EvaluationContractError, TypeError) as exc:
            raise CheckpointProtocolError(
                "final_evaluation_invalid",
                "Final evaluation result does not satisfy the contract.",
            ) from exc
        if result["execution_status"] != "completed":
            raise CheckpointProtocolError(
                "final_evaluation_invalid",
                "Final evaluation result has an invalid execution status.",
            )
        return result

    def _finish_managed(self, managed: ManagedProcess, exit_code: int) -> None:
        final_result = None
        if exit_code == 0 and self.command_builder is _pipeline_launch_spec:
            try:
                final_result = self._final_evaluation_result(managed.job_id)
            except CheckpointProtocolError as exc:
                self._fail_managed(
                    managed,
                    exc.code,
                    event_type="job.final_state_rejected",
                    exit_code=exit_code,
                    cancel_command_error_code="already_finished",
                )
                return
        self.repository.finish_process(
            managed.job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
            launch_token=managed.launch_token,
            exit_code=exit_code,
            evaluation_status=(
                None if final_result is None else str(final_result["evaluation_status"])
            ),
            quality_status=(
                None if final_result is None else str(final_result["quality_status"])
            ),
        )
        self._forget(managed.job_id)

    def _record_launch_failure(
        self,
        job_id: str,
        launch_token: str,
        *,
        failure_code: str = "process_launch_failed",
        event_type: str = "job.process_launch_failed",
    ) -> None:
        self.repository.fail_process(
            job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
            launch_token=launch_token,
            failure_code=failure_code,
            event_type=event_type,
        )

    @staticmethod
    def _launch_spec_is_valid(launch: LaunchSpec) -> bool:
        return bool(
            isinstance(launch.command, list)
            and launch.command
            and all(isinstance(part, str) for part in launch.command)
            and isinstance(launch.command_summary, str)
            and launch.command_summary
            and len(launch.command_summary) <= 1024
            and launch.command_summary.isprintable()
        )

    def _repair_context(self, job_id: str, repair_round: int) -> tuple[str, list[str]]:
        run_dir = job_service.RUNS_DIR / job_id
        feedback_path = run_dir / "output" / "eval_feedback.json"
        try:
            feedback = _safe_load_json(feedback_path)
            if isinstance(feedback, dict):
                decision_input = dict(feedback)
                decision_input["repair_round"] = max(repair_round - 1, 0)
                decision = decide_repair_action(decision_input)
                files = list(decision.get("files_to_fix") or [])
                reason = str(decision.get("reason") or "quality_rejected")
                if files or reason:
                    return reason, files
        except Exception:
            pass

        status_path = run_dir / "output" / "repo_status.json"
        try:
            repo_status = _safe_load_json(status_path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            repo_status = {}
        if not isinstance(repo_status, dict):
            repo_status = {}
        raw_files = (
            repo_status.get("files_to_fix")
            or repo_status.get("files_to_repair")
            or repo_status.get("repaired_files")
            or []
        )
        files = [str(item) for item in raw_files] if isinstance(raw_files, list) else []
        return str(repo_status.get("repair_reason") or "quality_rejected"), files

    def _sync_repair_attempt_checkpoint(
        self,
        managed: ManagedProcess,
        checkpoint: dict[str, object],
    ) -> None:
        if checkpoint.get("stage_name") != "repair":
            return
        stage_sequence = int(checkpoint["stage_sequence"])
        repair_round = (stage_sequence - 5) // 2
        if repair_round < 1:
            raise ValueError("repair checkpoint sequence is invalid.")
        reason, files_to_fix = self._repair_context(managed.job_id, repair_round)
        status = str(checkpoint["status"])
        if status == "running":
            self.repository.start_repair_attempt(
                managed.job_id,
                worker_id=self.worker_id,
                instance_token=self.instance_token,
                launch_token=managed.launch_token,
                attempt=repair_round,
                reason=reason,
                files_to_fix=files_to_fix,
            )
        elif status == "completed":
            self.repository.complete_repair_attempt(
                managed.job_id,
                worker_id=self.worker_id,
                instance_token=self.instance_token,
                launch_token=managed.launch_token,
                attempt=repair_round,
                reason=reason,
                result="pending_evaluation",
                files_to_fix=files_to_fix,
            )
        elif status == "failed":
            self.repository.fail_repair_attempt(
                managed.job_id,
                worker_id=self.worker_id,
                instance_token=self.instance_token,
                launch_token=managed.launch_token,
                attempt=repair_round,
                reason=str(checkpoint.get("error_code") or "stage_execution_failed"),
                result="repair_failed",
                files_to_fix=files_to_fix,
            )

    def _sync_process_checkpoints(self, managed: ManagedProcess) -> None:
        run_dir = job_service.RUNS_DIR / managed.job_id
        known_paths = {
            str(stage_run["checkpoint_path"])
            for stage_run in self.repository.list_stage_runs(managed.job_id)
            if stage_run.get("checkpoint_path")
        }
        for checkpoint, checkpoint_path in list_checkpoints(
            run_dir,
            expected_job_id=managed.job_id,
        ):
            existing = checkpoint_path in known_paths
            if existing:
                matching = next(
                    (
                        stage_run
                        for stage_run in self.repository.list_stage_runs(managed.job_id)
                        if stage_run.get("checkpoint_path") == checkpoint_path
                    ),
                    None,
                )
                if matching is not None and matching.get("status") == checkpoint["status"]:
                    self._sync_repair_attempt_checkpoint(managed, checkpoint)
                    continue
            self.repository.record_stage_checkpoint(
                managed.job_id,
                worker_id=self.worker_id,
                instance_token=self.instance_token,
                launch_token=managed.launch_token,
                checkpoint=checkpoint,
                checkpoint_path=checkpoint_path,
            )
            self._sync_repair_attempt_checkpoint(managed, checkpoint)
            known_paths.add(checkpoint_path)

    def _complete_confirmed_dead_cancellation(self, managed: ManagedProcess) -> None:
        """Complete cancellation only after this Worker has confirmed process death."""

        self.repository.claim_cancel_command(
            managed.job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
        )
        self.repository.complete_cancellation(
            managed.job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
            launch_token=managed.launch_token,
            exit_code=None,
        )
        self._forget(managed.job_id)

    def _handle_confirmed_process_exit(
        self,
        managed: ManagedProcess,
        *,
        observed_exit_code: int | None,
        has_local_process: bool,
        identity_confirmed_dead: bool,
        recovery_allowed: bool = True,
    ) -> bool:
        """Apply one exit/cancel/recovery contract to every confirmed-dead process."""

        if not identity_confirmed_dead:
            raise ValueError("Process exit handling requires confirmed process death.")
        cancel_command = self.repository.get_cancel_command(managed.job_id)
        cancel_pending = bool(
            cancel_command is not None
            and cancel_command["status"] in {"pending", "claimed"}
        )
        current_job = self.repository.get_job(managed.job_id)
        decision = ConfirmedExitDecision(
            has_local_process=has_local_process,
            observed_exit_code=observed_exit_code,
            cancel_pending=cancel_pending,
            identity_confirmed_dead=identity_confirmed_dead,
            recovery_allowed=recovery_allowed,
            recovery_count=int(current_job.get("recovery_count") or 0),
            max_recoveries=self.max_recoveries,
        )

        if decision.observed_exit_code == 0:
            self._finish_managed(managed, 0)
            return False
        if decision.cancel_pending:
            if decision.has_local_process and decision.observed_exit_code is not None:
                # A local poll is authoritative: natural exit wins over a late cancel.
                self._finish_managed(managed, decision.observed_exit_code)
            else:
                self._complete_confirmed_dead_cancellation(managed)
            return False
        if not decision.recovery_allowed:
            raise RuntimeError("Recovery was requested for a non-recoverable process state.")
        if decision.recovery_count >= decision.max_recoveries:
            self._fail_managed(
                managed,
                "recovery_attempts_exhausted",
                event_type="job.recovery_exhausted",
                exit_code=decision.observed_exit_code,
            )
            return False
        try:
            self._sync_process_checkpoints(managed)
            latest_completed = latest_completed_checkpoint(
                job_service.RUNS_DIR / managed.job_id,
                expected_job_id=managed.job_id,
            )
            if (
                latest_completed is not None
                and int(latest_completed[0]["stage_sequence"]) >= 2
            ):
                load_task_manifest(job_service.RUNS_DIR / managed.job_id / "output")
            if (
                latest_completed is not None
                and latest_completed[0]["stage_name"] == "completed"
            ):
                self._finish_managed(managed, 0)
                return False
            recovery = find_recovery_checkpoint(
                job_service.RUNS_DIR / managed.job_id,
                expected_job_id=managed.job_id,
            )
        except CheckpointProtocolError as exc:
            self._fail_managed(
                managed,
                exc.code,
                event_type="job.checkpoint_rejected",
                exit_code=decision.observed_exit_code,
            )
            return False
        except (TaskManifestError, OSError, UnicodeError, json.JSONDecodeError):
            self._fail_managed(
                managed,
                "checkpoint_manifest_invalid",
                event_type="job.checkpoint_rejected",
                exit_code=decision.observed_exit_code,
            )
            return False
        if recovery is None:
            self._fail_managed(
                managed,
                (
                    "checkpoint_resume_unavailable"
                    if latest_completed is not None
                    else "process_exited_without_checkpoint"
                ),
                event_type="job.reconciliation_failed",
                exit_code=decision.observed_exit_code,
            )
            return False

        if self.command_builder is _pipeline_launch_spec:
            try:
                if load_settings() is None:
                    raise SettingsNotConfiguredError()
                job_service.resolve_upload(str(current_job["upload_id"]))
            except (SettingsNotConfiguredError, ValueError, OSError):
                self._fail_managed(
                    managed,
                    "checkpoint_recovery_prerequisite_missing",
                    event_type="job.recovery_rejected",
                    exit_code=decision.observed_exit_code,
                )
                return False

        new_launch_token = uuid.uuid4().hex
        recovered_job = self.repository.prepare_recovery_attempt(
            managed.job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
            launch_token=managed.launch_token,
            new_launch_token=new_launch_token,
            resume_from_stage=recovery.resume_from_stage,
            resume_stage_sequence=recovery.resume_sequence,
            resume_stage_attempt=recovery.resume_attempt,
            max_recoveries=self.max_recoveries,
            observed_exit_code=decision.observed_exit_code,
        )
        self._forget(managed.job_id)
        recovered_job.update(
            {
                "_resume_from_stage": recovery.resume_from_stage,
                "_resume_stage_sequence": recovery.resume_sequence,
                "_resume_stage_attempt": recovery.resume_attempt,
                "_recovery_count": int(recovered_job["recovery_count"]),
                "_skip_mineru_for_resume": False,
            }
        )
        self._launch(recovered_job, new_launch_token)
        return True

    def _recover_missing_process(self, managed: ManagedProcess) -> bool:
        """Compatibility wrapper for a registered identity confirmed missing."""

        return self._handle_confirmed_process_exit(
            managed,
            observed_exit_code=None,
            has_local_process=False,
            identity_confirmed_dead=True,
        )

    def _reconcile(self) -> ReconcileResult:
        self._detach_all()
        unresolved_job_ids: list[str] = []
        recovering_job_ids: list[str] = []
        for process in self.repository.list_running_processes():
            job_id = str(process["job_id"])
            launch_token = process.get("launch_token")
            pid = process.get("pid")
            create_time = process.get("process_create_time")
            process_group_id = process.get("process_group_id")
            if not launch_token:
                raise RuntimeError(f"Running job {job_id} has no launch token.")
            if not pid or not create_time or not process_group_id:
                self.repository.mark_launch_identity_unresolved(
                    job_id,
                    worker_id=self.worker_id,
                    instance_token=self.instance_token,
                    launch_token=str(launch_token),
                )
                unresolved_job_ids.append(job_id)
                continue
            managed = ManagedProcess(
                job_id=job_id,
                launch_token=str(launch_token),
                pid=int(pid),
                process_create_time=str(create_time),
                process_group_id=int(process_group_id),
            )
            identity = process_identity_status(int(pid), str(create_time))
            if identity == "mismatch":
                self._fail_managed(
                    managed,
                    "process_identity_mismatch",
                    event_type="job.process_identity_mismatch",
                )
                continue
            if identity == "missing":
                if self._recover_missing_process(managed):
                    recovering_job_ids.append(job_id)
                continue
            self._managed[job_id] = managed
            self.repository.heartbeat_process(
                job_id,
                worker_id=self.worker_id,
                instance_token=self.instance_token,
                launch_token=managed.launch_token,
            )
        result = ReconcileResult(
            safe_to_schedule=not unresolved_job_ids and not recovering_job_ids,
            unresolved_job_ids=tuple(unresolved_job_ids),
            recovering_job_ids=tuple(recovering_job_ids),
        )
        self._unresolved_job_ids = result.unresolved_job_ids
        self._last_reconcile_result = result
        self._reconciled = True
        return result

    def _remember_unresolved_launch(self, job_id: str) -> None:
        self._unresolved_job_ids = tuple(
            sorted({*self._unresolved_job_ids, job_id})
        )
        self._last_reconcile_result = ReconcileResult(
            safe_to_schedule=False,
            unresolved_job_ids=self._unresolved_job_ids,
            recovering_job_ids=(),
        )

    def _cancel_before_launch(self, job: dict[str, object], launch_token: str) -> bool:
        command = self.repository.get_cancel_command(str(job["job_id"]))
        if command is None or command["status"] not in {"pending", "claimed"}:
            return False
        self.repository.claim_cancel_command(
            str(job["job_id"]),
            worker_id=self.worker_id,
            instance_token=self.instance_token,
        )
        self.repository.complete_cancellation(
            str(job["job_id"]),
            worker_id=self.worker_id,
            instance_token=self.instance_token,
            launch_token=launch_token,
            exit_code=None,
        )
        return True

    def _launch(self, job: dict[str, object], launch_token: str) -> None:
        if self._cancel_before_launch(job, launch_token):
            return
        job_id = str(job["job_id"])
        try:
            launch = self.command_builder(job)
        except job_service.ProviderSettingsChangedError:
            self._record_launch_failure(
                job_id,
                launch_token,
                failure_code="provider_settings_changed",
                event_type="job.provider_settings_changed",
            )
            return
        except (SettingsNotConfiguredError, ValueError, OSError):
            self._record_launch_failure(job_id, launch_token)
            return
        if not self._launch_spec_is_valid(launch):
            self._record_launch_failure(job_id, launch_token)
            return
        if self._cancel_before_launch(job, launch_token):
            return

        logs_dir = job_service.RUNS_DIR / job_id / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        launcher_log = logs_dir / "00_worker_pipeline.log"
        log_file = open(launcher_log, "w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                launch.command,
                cwd=str(REPO_ROOT),
                env=launch.environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **job_service.pipeline_popen_kwargs(),
            )
        except (OSError, ValueError):
            log_file.close()
            self._record_launch_failure(job_id, launch_token)
            return

        create_time = get_process_create_time(process.pid, process)
        if create_time is None:
            self._remember_unresolved_launch(job_id)
            try:
                self.repository.mark_launch_identity_unresolved(
                    job_id,
                    worker_id=self.worker_id,
                    instance_token=self.instance_token,
                    launch_token=launch_token,
                )
            finally:
                log_file.close()
            raise RuntimeError("The pipeline process identity could not be recorded.")

        managed = ManagedProcess(
            job_id=job_id,
            launch_token=launch_token,
            pid=process.pid,
            process_create_time=create_time,
            process_group_id=job_service.process_group_id(process),
            process=process,
            log_file=log_file,
        )
        try:
            self.repository.record_process_started(
                job_id,
                worker_id=self.worker_id,
                instance_token=self.instance_token,
                launch_token=launch_token,
                pid=managed.pid,
                process_create_time=managed.process_create_time,
                process_group_id=managed.process_group_id,
                command_summary=launch.command_summary,
            )
        except Exception:
            self._remember_unresolved_launch(job_id)
            try:
                termination = terminate_verified_process_tree(
                    pid=managed.pid,
                    expected_create_time=managed.process_create_time,
                    process_group_id=managed.process_group_id,
                    graceful_timeout=min(self.cancel_grace_seconds, 3.0),
                )
                if termination.terminated:
                    self.repository.fail_process(
                        job_id,
                        worker_id=self.worker_id,
                        instance_token=self.instance_token,
                        launch_token=launch_token,
                        failure_code="process_registration_failed",
                        event_type="job.process_registration_failed",
                        exit_code=process.poll(),
                    )
                    self._unresolved_job_ids = tuple(
                        unresolved_job_id
                        for unresolved_job_id in self._unresolved_job_ids
                        if unresolved_job_id != job_id
                    )
                    self._last_reconcile_result = ReconcileResult(
                        safe_to_schedule=not self._unresolved_job_ids,
                        unresolved_job_ids=self._unresolved_job_ids,
                    )
                else:
                    self.repository.mark_launch_identity_unresolved(
                        job_id,
                        worker_id=self.worker_id,
                        instance_token=self.instance_token,
                        launch_token=launch_token,
                    )
            finally:
                log_file.close()
            raise
        self._managed[job_id] = managed

    def _cancel_managed(self, managed: ManagedProcess) -> None:
        if not self.repository.acquire_worker_lease(
            self.worker_id,
            self.instance_token,
            lease_seconds=self.lease_seconds,
        ):
            raise RuntimeError("The worker could not renew the global lease before canceling.")
        command = self.repository.claim_cancel_command(
            managed.job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
        )
        if command is None or command["status"] not in {"pending", "claimed"}:
            return
        result = terminate_verified_process_tree(
            pid=managed.pid,
            expected_create_time=managed.process_create_time,
            process_group_id=managed.process_group_id,
            graceful_timeout=self.cancel_grace_seconds,
            force_timeout=FORCE_CANCEL_SECONDS,
        )
        if result.reason == "process_identity_mismatch":
            self._fail_managed(
                managed,
                "process_identity_mismatch",
                event_type="job.process_identity_mismatch",
            )
            return
        if result.reason == "process_missing":
            if managed.process is not None:
                exit_code = managed.process.poll()
                if exit_code is not None:
                    self._handle_confirmed_process_exit(
                        managed,
                        observed_exit_code=exit_code,
                        has_local_process=True,
                        identity_confirmed_dead=True,
                    )
                    return
            self._fail_managed(
                managed,
                "process_exited_without_checkpoint",
                event_type="job.reconciliation_failed",
            )
            return
        if not result.terminated:
            raise RuntimeError(
                f"Failed to terminate process tree for {managed.job_id}: {result.reason}"
            )
        exit_code = None
        if managed.process is not None:
            try:
                exit_code = managed.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                exit_code = managed.process.poll()
        self.repository.complete_cancellation(
            managed.job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
            launch_token=managed.launch_token,
            exit_code=exit_code,
        )
        self._forget(managed.job_id)

    def _monitor_managed(self) -> None:
        for managed in list(self._managed.values()):
            self._sync_process_checkpoints(managed)
            if managed.process is not None:
                exit_code = managed.process.poll()
                if exit_code is not None:
                    self._handle_confirmed_process_exit(
                        managed,
                        observed_exit_code=exit_code,
                        has_local_process=True,
                        identity_confirmed_dead=True,
                    )
                    continue

            command = self.repository.get_cancel_command(managed.job_id)
            if command is not None and command["status"] in {"pending", "claimed"}:
                self._cancel_managed(managed)
                continue

            if managed.process is None:
                identity = process_identity_status(
                    managed.pid,
                    managed.process_create_time,
                )
                if identity == "mismatch":
                    self._fail_managed(
                        managed,
                        "process_identity_mismatch",
                        event_type="job.process_identity_mismatch",
                    )
                    continue
                if identity == "missing":
                    self._recover_missing_process(managed)
                    continue
            self.repository.heartbeat_process(
                managed.job_id,
                worker_id=self.worker_id,
                instance_token=self.instance_token,
                launch_token=managed.launch_token,
            )

    def run_once(self) -> bool:
        lease_owned = self.repository.acquire_worker_lease(
            self.worker_id,
            self.instance_token,
            lease_seconds=self.lease_seconds,
        )
        if not lease_owned:
            self._detach_all()
            self._unresolved_job_ids = ()
            self._last_reconcile_result = ReconcileResult(True, ())
            self._lease_owned = False
            self._reconciled = False
            return False
        self._lease_owned = True
        if not self._reconciled or self._unresolved_job_ids:
            reconciliation = self._reconcile()
            if not reconciliation.safe_to_schedule:
                return True

        while (
            self.repository.cancel_next_queued_job(
                self.worker_id, self.instance_token
            )
            is not None
        ):
            pass
        self._monitor_managed()
        if len(self._managed) < self.max_concurrency:
            launch_token = uuid.uuid4().hex
            job = self.repository.claim_next_queued_job(
                worker_id=self.worker_id,
                instance_token=self.instance_token,
                launch_token=launch_token,
            )
            if job is not None:
                self._launch(job, launch_token)
        return True

    def run(self, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or threading.Event()
        try:
            while not stop.is_set():
                self.run_once()
                stop.wait(self.poll_interval)
        finally:
            self.close()

    def close(self) -> None:
        if self._lease_owned:
            self.repository.release_worker_lease(
                self.worker_id, self.instance_token
            )
        self._lease_owned = False
        self._reconciled = False
        self._unresolved_job_ids = ()
        self._last_reconcile_result = ReconcileResult(True, ())
        for job_id in list(self._managed):
            self._forget(job_id)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Paper2Code SQLite worker.")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--lease-seconds", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if configured_job_runtime() != "sqlite":
        raise SystemExit("Set JOB_RUNTIME=sqlite before starting web_api.worker.")
    worker = PipelineWorker(
        worker_id=args.worker_id,
        max_concurrency=args.max_concurrency,
        poll_interval=args.poll_interval,
        lease_seconds=args.lease_seconds,
    )
    stop_event = threading.Event()

    def request_stop(signum, frame) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    worker.run(stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
