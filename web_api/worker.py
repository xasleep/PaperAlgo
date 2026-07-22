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


CommandBuilder = Callable[[dict[str, object]], LaunchSpec]
FORCE_CANCEL_SECONDS = 5.0


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _pipeline_launch_spec(job: dict[str, object]) -> LaunchSpec:
    settings = load_settings()
    if settings is None:
        raise SettingsNotConfiguredError()
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
        skip_mineru=bool(job["skip_mineru"]),
        pdf_markdown_path=str(job["pdf_markdown_path"]),
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
        self.repository = repository or JobRepository()
        self.worker_id = worker_id or _default_worker_id()
        self.instance_token = instance_token or uuid.uuid4().hex
        self.max_concurrency = max_concurrency
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.cancel_grace_seconds = cancel_grace_seconds
        self.command_builder = command_builder or _pipeline_launch_spec
        self._managed: dict[str, ManagedProcess] = {}
        self._unresolved_job_ids: tuple[str, ...] = ()
        self._last_reconcile_result = ReconcileResult(True, ())
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
    ) -> None:
        self.repository.fail_process(
            managed.job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
            launch_token=managed.launch_token,
            failure_code=failure_code,
            event_type=event_type,
            exit_code=exit_code,
        )
        self._forget(managed.job_id)

    def _finish_managed(self, managed: ManagedProcess, exit_code: int) -> None:
        self.repository.finish_process(
            managed.job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
            launch_token=managed.launch_token,
            exit_code=exit_code,
        )
        self._forget(managed.job_id)

    def _record_launch_failure(self, job_id: str, launch_token: str) -> None:
        self.repository.fail_process(
            job_id,
            worker_id=self.worker_id,
            instance_token=self.instance_token,
            launch_token=launch_token,
            failure_code="process_launch_failed",
            event_type="job.process_launch_failed",
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

    def _reconcile(self) -> ReconcileResult:
        self._detach_all()
        unresolved_job_ids: list[str] = []
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
                self._fail_managed(
                    managed,
                    "process_exited_without_checkpoint",
                    event_type="job.reconciliation_failed",
                )
                continue
            self._managed[job_id] = managed
            self.repository.heartbeat_process(
                job_id,
                worker_id=self.worker_id,
                instance_token=self.instance_token,
                launch_token=managed.launch_token,
            )
        result = ReconcileResult(
            safe_to_schedule=not unresolved_job_ids,
            unresolved_job_ids=tuple(unresolved_job_ids),
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
                    self._finish_managed(managed, exit_code)
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
            if managed.process is not None:
                exit_code = managed.process.poll()
                if exit_code is not None:
                    self._finish_managed(managed, exit_code)
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
                    self._fail_managed(
                        managed,
                        "process_exited_without_checkpoint",
                        event_type="job.reconciliation_failed",
                    )
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
