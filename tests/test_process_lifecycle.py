import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from web_api import job_service, process_identity as process_identity_module
from web_api.errors import JobNotCancelableError
from web_api.process_identity import ProcessIdentity


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _spawn_parent_with_child(tmp_path: Path) -> tuple[subprocess.Popen, int]:
    child_pid_path = tmp_path / f"child_{time.monotonic_ns()}.pid"
    parent_script = tmp_path / f"parent_{time.monotonic_ns()}.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(parent_script), str(child_pid_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **job_service.pipeline_popen_kwargs(),
    )
    assert _wait_until(child_pid_path.exists)
    return proc, int(child_pid_path.read_text(encoding="utf-8"))


def _write_status(runs_dir: Path, job_id: str, **status: object) -> Path:
    run_dir = runs_dir / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    data = {"job_id": job_id, **status}
    (run_dir / "run_status.json").write_text(
        json.dumps(data),
        encoding="utf-8",
    )
    return run_dir


def _cleanup_process(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        job_service.terminate_process_tree(proc.pid, proc, timeout=3.0)


@pytest.fixture(autouse=True)
def _clear_active_processes():
    job_service.ACTIVE_PROCESSES.clear()
    job_service.ACTIVE_LOG_FILES.clear()
    yield
    job_service.ACTIVE_PROCESSES.clear()
    job_service.ACTIVE_LOG_FILES.clear()


def _int_value(value: object) -> int:
    raw = getattr(value, "value", value)
    assert raw is not None
    return int(raw)


class _FakeKernel32:
    def __init__(self, process_create_times: dict[int, str]) -> None:
        self.process_create_times = process_create_times
        self.handles: dict[int, int] = {}
        self.open_calls: list[tuple[int, int]] = []
        self.time_calls: list[int] = []
        self.exit_code_calls: list[int] = []
        self.terminate_calls: list[int] = []
        self.wait_calls: list[tuple[int, int]] = []
        self.close_calls: list[int] = []
        self.fail_open_for: set[int] = set()
        self.fail_times_for: set[int] = set()
        self.fail_exit_code_for: set[int] = set()
        self.fail_terminate_for: set[int] = set()
        self.wait_result_for: dict[int, int] = {}

    def OpenProcess(self, access, inherit_handle, pid):
        del inherit_handle
        pid_value = _int_value(pid)
        self.open_calls.append((_int_value(access), pid_value))
        if pid_value in self.fail_open_for or pid_value not in self.process_create_times:
            return 0
        handle = pid_value + 100_000
        self.handles[handle] = pid_value
        return handle

    def GetProcessTimes(
        self,
        handle,
        creation,
        exit_time,
        kernel_time,
        user_time,
    ):
        del exit_time, kernel_time, user_time
        handle_value = _int_value(handle)
        pid = self.handles[handle_value]
        self.time_calls.append(handle_value)
        if pid in self.fail_times_for:
            return 0
        create_time = int(self.process_create_times[pid])
        creation._obj.low = create_time & 0xFFFFFFFF
        creation._obj.high = create_time >> 32
        return 1

    def GetExitCodeProcess(self, handle, exit_code):
        handle_value = _int_value(handle)
        pid = self.handles[handle_value]
        self.exit_code_calls.append(handle_value)
        if pid in self.fail_exit_code_for:
            return 0
        exit_code._obj.value = 259
        return 1

    def TerminateProcess(self, handle, exit_code):
        del exit_code
        handle_value = _int_value(handle)
        pid = self.handles[handle_value]
        self.terminate_calls.append(handle_value)
        return 0 if pid in self.fail_terminate_for else 1

    def WaitForSingleObject(self, handle, timeout_ms):
        handle_value = _int_value(handle)
        pid = self.handles[handle_value]
        self.wait_calls.append((handle_value, _int_value(timeout_ms)))
        return self.wait_result_for.get(pid, 0)

    def CloseHandle(self, handle):
        self.close_calls.append(_int_value(handle))
        return 1


def test_windows_force_uses_same_verified_handle_and_closes_it(
) -> None:
    kernel32 = _FakeKernel32({501: "123456"})

    result = process_identity_module._terminate_windows_identity(
        ProcessIdentity(pid=501, create_time="123456"),
        timeout=0.25,
        kernel32=kernel32,
    )

    handle = 100_501
    assert result.terminated is True
    assert result.reason == "process_tree_terminated"
    assert kernel32.open_calls == [(0x1000 | 0x0001 | 0x00100000, 501)]
    assert kernel32.time_calls == [handle]
    assert kernel32.exit_code_calls == [handle]
    assert kernel32.terminate_calls == [handle]
    assert kernel32.wait_calls == [(handle, 250)]
    assert kernel32.close_calls == [handle]


def test_windows_force_revalidates_create_time_on_handle_before_terminate() -> None:
    kernel32 = _FakeKernel32({502: "999999"})

    result = process_identity_module._terminate_windows_identity(
        ProcessIdentity(pid=502, create_time="original"),
        timeout=0.25,
        kernel32=kernel32,
    )

    assert result.terminated is False
    assert result.reason == "process_identity_mismatch"
    assert kernel32.terminate_calls == []
    assert kernel32.close_calls == [100_502]


@pytest.mark.parametrize(
    ("failure", "expected_reason", "expected_close_count"),
    [
        ("open", "process_tree_termination_failed", 0),
        ("times", "process_tree_termination_failed", 1),
        ("exit_code", "process_tree_termination_failed", 1),
        ("terminate", "process_tree_termination_failed", 1),
        ("timeout", "process_termination_timeout", 1),
    ],
)
def test_windows_force_failures_are_bounded_explicit_and_close_handles(
    failure: str,
    expected_reason: str,
    expected_close_count: int,
) -> None:
    kernel32 = _FakeKernel32({503: "503000"})
    if failure == "open":
        kernel32.fail_open_for.add(503)
    elif failure == "times":
        kernel32.fail_times_for.add(503)
    elif failure == "exit_code":
        kernel32.fail_exit_code_for.add(503)
    elif failure == "terminate":
        kernel32.fail_terminate_for.add(503)
    elif failure == "timeout":
        kernel32.wait_result_for[503] = 258

    result = process_identity_module._terminate_windows_identity(
        ProcessIdentity(pid=503, create_time="503000"),
        timeout=0.01,
        kernel32=kernel32,
    )

    assert result.terminated is False
    assert result.reason == expected_reason
    assert len(kernel32.close_calls) == expected_close_count


def test_windows_wait_observation_does_not_treat_open_failure_as_exit() -> None:
    kernel32 = _FakeKernel32({504: "504000"})
    kernel32.fail_open_for.add(504)

    status = process_identity_module._windows_identity_status_for_wait(
        ProcessIdentity(pid=504, create_time="504000"),
        kernel32=kernel32,
    )

    assert status == "unresolved"
    assert kernel32.close_calls == []


def test_windows_tree_force_skips_reused_child_without_terminating_it() -> None:
    kernel32 = _FakeKernel32({601: "601000", 602: "999999"})
    identities = [
        ProcessIdentity(pid=601, create_time="601000"),
        ProcessIdentity(pid=602, create_time="602000"),
    ]

    result = process_identity_module._force_windows_process_tree(
        identities,
        timeout=0.25,
        kernel32=kernel32,
    )

    assert result.terminated is True
    assert result.reason == "process_tree_terminated"
    assert 100_601 in kernel32.terminate_calls
    assert 100_602 not in kernel32.terminate_calls
    assert sorted(kernel32.close_calls) == [100_601, 100_602]


def test_windows_tree_force_fails_if_any_verified_member_cannot_terminate() -> None:
    kernel32 = _FakeKernel32({611: "611000", 612: "612000"})
    kernel32.fail_terminate_for.add(612)

    result = process_identity_module._force_windows_process_tree(
        [
            ProcessIdentity(pid=611, create_time="611000"),
            ProcessIdentity(pid=612, create_time="612000"),
        ],
        timeout=0.25,
        kernel32=kernel32,
    )

    assert result.terminated is False
    assert result.reason == "process_tree_termination_failed"
    assert sorted(kernel32.close_calls) == [100_612]


def test_windows_graceful_signal_requires_root_process_group_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProcessIdentity(pid=701, create_time="701000")
    signal_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(process_identity_module.sys, "platform", "win32")
    monkeypatch.setattr(
        process_identity_module,
        "process_identity_status",
        lambda pid, create_time: "matching",
    )
    monkeypatch.setattr(
        process_identity_module,
        "_snapshot_process_tree",
        lambda identity: [root],
    )
    monkeypatch.setattr(
        process_identity_module.os,
        "kill",
        lambda pid, sig: signal_calls.append((pid, sig)),
    )

    result = process_identity_module.terminate_verified_process_tree(
        pid=root.pid,
        expected_create_time=root.create_time,
        process_group_id=999,
        graceful_timeout=0,
        force_timeout=0,
    )

    assert result.terminated is False
    assert result.reason == "process_identity_mismatch"
    assert signal_calls == []


def test_root_identity_change_after_snapshot_sends_no_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProcessIdentity(pid=702, create_time="702000")
    statuses = iter(("matching", "mismatch"))
    signal_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        process_identity_module,
        "process_identity_status",
        lambda pid, create_time: next(statuses),
    )
    monkeypatch.setattr(
        process_identity_module,
        "_snapshot_process_tree",
        lambda identity: [root],
    )
    monkeypatch.setattr(
        process_identity_module.os,
        "kill",
        lambda pid, sig: signal_calls.append((pid, sig)),
    )

    result = process_identity_module.terminate_verified_process_tree(
        pid=root.pid,
        expected_create_time=root.create_time,
        process_group_id=root.pid,
        graceful_timeout=0,
        force_timeout=0,
    )

    assert result.terminated is False
    assert result.reason == "process_identity_mismatch"
    assert signal_calls == []


def test_posix_graceful_signal_requires_matching_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProcessIdentity(pid=703, create_time="703000")
    killpg_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(process_identity_module.sys, "platform", "linux")
    monkeypatch.setattr(
        process_identity_module,
        "process_identity_status",
        lambda pid, create_time: "matching",
    )
    monkeypatch.setattr(
        process_identity_module,
        "_snapshot_process_tree",
        lambda identity: [root],
    )
    monkeypatch.setattr(
        process_identity_module.os,
        "getpgid",
        lambda pid: 999,
        raising=False,
    )
    monkeypatch.setattr(
        process_identity_module.os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
        raising=False,
    )

    result = process_identity_module.terminate_verified_process_tree(
        pid=root.pid,
        expected_create_time=root.create_time,
        process_group_id=root.pid,
        graceful_timeout=0,
        force_timeout=0,
    )

    assert result.terminated is False
    assert result.reason == "process_identity_mismatch"
    assert killpg_calls == []


def test_cancel_job_terminates_child_process_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    proc = None
    child_pid = None
    try:
        proc, child_pid = _spawn_parent_with_child(tmp_path)
        _write_status(
            runs_dir,
            "tree_job",
            status="running",
            process_pid=proc.pid,
            pid=proc.pid,
            process_group_id=job_service.process_group_id(proc),
            process_state="active",
            cancelable=True,
        )
        job_service.ACTIVE_PROCESSES["tree_job"] = proc

        canceled, message = job_service.cancel_job("tree_job")

        assert canceled is True
        assert "terminated" in message or "killed" in message
        assert _wait_until(lambda: proc.poll() is not None)
        assert _wait_until(lambda: not job_service.is_pid_alive(child_pid))
        status = job_service.read_json_file(runs_dir / "tree_job" / "run_status.json")
        assert status["status"] == "canceled"
        assert status["process_state"] == "finished"
        assert status["process_active"] is False
        assert status["cancelable"] is False
    finally:
        if child_pid and job_service.is_pid_alive(child_pid):
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
        _cleanup_process(proc)


def test_get_job_status_distinguishes_process_lifecycle_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    active_proc = None
    detached_proc = None
    try:
        active_proc, _ = _spawn_parent_with_child(tmp_path)
        _write_status(
            runs_dir,
            "active_job",
            status="running",
            process_pid=active_proc.pid,
            pid=active_proc.pid,
        )
        job_service.ACTIVE_PROCESSES["active_job"] = active_proc

        detached_proc, _ = _spawn_parent_with_child(tmp_path)
        _write_status(
            runs_dir,
            "detached_job",
            status="running",
            process_pid=detached_proc.pid,
            pid=detached_proc.pid,
        )

        missing_pid = 2_147_483_647
        _write_status(
            runs_dir,
            "orphaned_job",
            status="running",
            process_pid=missing_pid,
            pid=missing_pid,
        )
        _write_status(
            runs_dir,
            "finished_job",
            status="completed",
            process_pid=missing_pid,
            pid=missing_pid,
        )

        active = job_service.get_job_status("active_job")
        detached = job_service.get_job_status("detached_job")
        orphaned = job_service.get_job_status("orphaned_job")
        finished = job_service.get_job_status("finished_job")

        assert active["process_state"] == "active"
        assert active["process_active"] is True
        assert active["cancelable"] is True

        assert detached["process_state"] == "detached"
        assert detached["process_active"] is False
        assert detached["cancelable"] is False
        with pytest.raises(JobNotCancelableError) as exc_info:
            job_service.cancel_job("detached_job")
        assert exc_info.value.reason == "detached_after_restart"

        assert orphaned["process_state"] == "orphaned"
        assert orphaned["process_active"] is False
        assert orphaned["cancelable"] is False

        assert finished["process_state"] == "finished"
        assert finished["process_active"] is False
        assert finished["cancelable"] is False
    finally:
        _cleanup_process(active_proc)
        _cleanup_process(detached_proc)
