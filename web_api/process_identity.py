from __future__ import annotations

import ctypes
import math
import os
import signal
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: str


@dataclass(frozen=True)
class TerminationResult:
    terminated: bool
    reason: str


_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_PROCESS_TERMINATE = 0x0001
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_STILL_ACTIVE = 259
_WINDOWS_WAIT_OBJECT_0 = 0
_WINDOWS_WAIT_TIMEOUT = 258


class _WindowsFileTime(ctypes.Structure):
    _fields_ = [
        ("low", wintypes.DWORD),
        ("high", wintypes.DWORD),
    ]


def _load_kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsFileTime),
        ctypes.POINTER(_WindowsFileTime),
        ctypes.POINTER(_WindowsFileTime),
        ctypes.POINTER(_WindowsFileTime),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _windows_create_time_from_open_handle(kernel32: Any, handle: Any) -> str | None:
    creation = _WindowsFileTime()
    exit_time = _WindowsFileTime()
    kernel_time = _WindowsFileTime()
    user_time = _WindowsFileTime()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        return None
    return str((int(creation.high) << 32) | int(creation.low))


def _windows_create_time_from_handle(handle: int) -> str | None:
    return _windows_create_time_from_open_handle(
        _load_kernel32(),
        wintypes.HANDLE(handle),
    )


def _windows_process_create_time(pid: int) -> str | None:
    kernel32 = _load_kernel32()
    handle = kernel32.OpenProcess(
        _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != _WINDOWS_STILL_ACTIVE:
            return None
        return _windows_create_time_from_open_handle(kernel32, handle)
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_stat(pid: int) -> tuple[int, str] | None:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        value = stat_path.read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    closing_parenthesis = value.rfind(")")
    if closing_parenthesis < 0:
        return None
    fields = value[closing_parenthesis + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        parent_pid = int(fields[1])
    except ValueError:
        return None
    return parent_pid, fields[19]


def get_process_create_time(pid: int, process: Any | None = None) -> str | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if sys.platform == "win32":
        handle = getattr(process, "_handle", None)
        if handle:
            create_time = _windows_create_time_from_handle(int(handle))
            if create_time is not None:
                return create_time
        return _windows_process_create_time(pid)
    stat = _posix_process_stat(pid)
    return stat[1] if stat is not None else None


def process_identity_status(pid: int, expected_create_time: str) -> str:
    current = get_process_create_time(pid)
    if current is None:
        return "missing" if not _pid_is_alive(pid) else "mismatch"
    return "matching" if current == expected_create_time else "mismatch"


def _pid_is_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _windows_process_create_time(pid) is not None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _windows_parent_map() -> dict[int, int]:
    import ctypes
    from ctypes import wintypes

    max_path = 260
    invalid_handle_value = ctypes.c_void_p(-1).value
    th32cs_snapprocess = 0x00000002

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * max_path),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(th32cs_snapprocess, 0)
    if int(snapshot) == invalid_handle_value:
        return {}
    parents: dict[int, int] = {}
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        if not process_first(snapshot, ctypes.byref(entry)):
            return parents
        while True:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            if not process_next(snapshot, ctypes.byref(entry)):
                break
    finally:
        close_handle(snapshot)
    return parents


def _posix_parent_map() -> dict[int, int]:
    parents: dict[int, int] = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return parents
    for entry in entries:
        if not entry.name.isdigit():
            continue
        stat = _posix_process_stat(int(entry.name))
        if stat is not None:
            parents[int(entry.name)] = stat[0]
    return parents


def _snapshot_process_tree(root: ProcessIdentity) -> list[ProcessIdentity]:
    parents = _windows_parent_map() if sys.platform == "win32" else _posix_parent_map()
    descendants: list[int] = []
    frontier = [root.pid]
    seen = {root.pid}
    while frontier:
        parent = frontier.pop()
        children = [pid for pid, ppid in parents.items() if ppid == parent and pid not in seen]
        for child in children:
            seen.add(child)
            descendants.append(child)
            frontier.append(child)
    identities = [root]
    for pid in descendants:
        create_time = get_process_create_time(pid)
        if create_time is not None:
            identities.append(ProcessIdentity(pid=pid, create_time=create_time))
    return identities


def _windows_identity_status_for_wait(
    identity: ProcessIdentity,
    *,
    kernel32: Any | None = None,
) -> str:
    system_kernel32 = kernel32 is None
    kernel32 = kernel32 or _load_kernel32()
    handle = kernel32.OpenProcess(
        _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION | _WINDOWS_SYNCHRONIZE,
        False,
        identity.pid,
    )
    if not handle:
        if system_kernel32 and ctypes.get_last_error() == 87:
            return "missing"
        return "unresolved"
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return "unresolved"
        create_time = _windows_create_time_from_open_handle(kernel32, handle)
        if create_time is None:
            return "unresolved"
        if create_time != identity.create_time:
            return "mismatch"
        return (
            "matching"
            if exit_code.value == _WINDOWS_STILL_ACTIVE
            else "missing"
        )
    finally:
        kernel32.CloseHandle(handle)


def _identity_status_for_wait(identity: ProcessIdentity) -> str:
    if sys.platform == "win32":
        return _windows_identity_status_for_wait(identity)
    current_create_time = get_process_create_time(identity.pid)
    if current_create_time is None:
        return "missing"
    return "matching" if current_create_time == identity.create_time else "mismatch"


def _tree_has_exited(identities: list[ProcessIdentity]) -> bool:
    return all(
        _identity_status_for_wait(identity) in {"missing", "mismatch"}
        for identity in identities
    )


def _wait_for_tree_exit(identities: list[ProcessIdentity], timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if _tree_has_exited(identities):
            return True
        time.sleep(0.05)
    return _tree_has_exited(identities)


def _validated_process_group_status(
    root: ProcessIdentity,
    process_group_id: int,
) -> str:
    status = process_identity_status(root.pid, root.create_time)
    if status != "matching":
        return (
            "process_missing"
            if status == "missing"
            else "process_identity_mismatch"
        )
    if sys.platform == "win32":
        return (
            "matching"
            if process_group_id == root.pid
            else "process_identity_mismatch"
        )
    try:
        current_process_group = os.getpgid(root.pid)
    except ProcessLookupError:
        return "process_missing"
    except (PermissionError, OSError):
        return "process_tree_termination_failed"
    return (
        "matching"
        if current_process_group == process_group_id
        else "process_identity_mismatch"
    )


def _send_verified_group_signal(
    root: ProcessIdentity,
    process_group_id: int,
    sig: int,
) -> tuple[bool, str]:
    status = _validated_process_group_status(root, process_group_id)
    if status != "matching":
        return False, status
    try:
        if sys.platform == "win32":
            os.kill(process_group_id, sig)
        else:
            os.killpg(process_group_id, sig)
    except ProcessLookupError:
        return False, "process_missing"
    except (PermissionError, OSError):
        return False, "process_tree_termination_failed"
    return True, "signal_sent"


def _terminate_windows_identity(
    identity: ProcessIdentity,
    *,
    timeout: float,
    kernel32: Any | None = None,
) -> TerminationResult:
    kernel32 = kernel32 or _load_kernel32()
    access = (
        _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION
        | _WINDOWS_PROCESS_TERMINATE
        | _WINDOWS_SYNCHRONIZE
    )
    handle = kernel32.OpenProcess(access, False, identity.pid)
    if not handle:
        return TerminationResult(False, "process_tree_termination_failed")
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return TerminationResult(False, "process_tree_termination_failed")
        if exit_code.value != _WINDOWS_STILL_ACTIVE:
            return TerminationResult(False, "process_missing")
        create_time = _windows_create_time_from_open_handle(kernel32, handle)
        if create_time is None:
            return TerminationResult(False, "process_tree_termination_failed")
        if create_time != identity.create_time:
            return TerminationResult(False, "process_identity_mismatch")
        if not kernel32.TerminateProcess(handle, 1):
            return TerminationResult(False, "process_tree_termination_failed")
        timeout_ms = min(
            int(wintypes.DWORD(-1).value - 1),
            max(0, int(math.ceil(timeout * 1000))),
        )
        wait_result = int(kernel32.WaitForSingleObject(handle, timeout_ms))
        if wait_result == _WINDOWS_WAIT_OBJECT_0:
            return TerminationResult(True, "process_tree_terminated")
        if wait_result == _WINDOWS_WAIT_TIMEOUT:
            return TerminationResult(False, "process_termination_timeout")
        return TerminationResult(False, "process_tree_termination_failed")
    finally:
        kernel32.CloseHandle(handle)


def _force_windows_process_tree(
    identities: list[ProcessIdentity],
    *,
    timeout: float,
    kernel32: Any | None = None,
) -> TerminationResult:
    kernel32 = kernel32 or _load_kernel32()
    deadline = time.monotonic() + max(0.0, timeout)
    for identity in [*reversed(identities[1:]), identities[0]]:
        remaining = max(0.0, deadline - time.monotonic())
        result = _terminate_windows_identity(
            identity,
            timeout=remaining,
            kernel32=kernel32,
        )
        if result.terminated:
            continue
        if identity is not identities[0] and result.reason in {
            "process_identity_mismatch",
            "process_missing",
        }:
            continue
        return result
    return TerminationResult(True, "process_tree_terminated")


def _send_verified_posix_force(
    root: ProcessIdentity,
    process_group_id: int,
) -> tuple[bool, str]:
    return _send_verified_group_signal(root, process_group_id, signal.SIGKILL)


def terminate_verified_process_tree(
    *,
    pid: int,
    expected_create_time: str,
    process_group_id: int,
    graceful_timeout: float = 10.0,
    force_timeout: float = 5.0,
) -> TerminationResult:
    if not math.isfinite(graceful_timeout) or graceful_timeout < 0:
        raise ValueError("graceful_timeout must be a finite non-negative value.")
    if not math.isfinite(force_timeout) or force_timeout < 0:
        raise ValueError("force_timeout must be a finite non-negative value.")
    status = process_identity_status(pid, expected_create_time)
    if status == "mismatch":
        return TerminationResult(False, "process_identity_mismatch")
    if status == "missing":
        return TerminationResult(False, "process_missing")

    root = ProcessIdentity(pid=pid, create_time=expected_create_time)
    identities = _snapshot_process_tree(root)
    graceful_signal = (
        signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM
    )
    graceful_sent, graceful_reason = _send_verified_group_signal(
        root,
        process_group_id,
        graceful_signal,
    )
    if graceful_reason in {
        "process_identity_mismatch",
        "process_missing",
    }:
        return TerminationResult(False, graceful_reason)
    if graceful_sent and _wait_for_tree_exit(identities, graceful_timeout):
        return TerminationResult(True, "process_tree_terminated")

    if sys.platform == "win32":
        return _force_windows_process_tree(
            identities,
            timeout=force_timeout,
        )
    force_sent, force_reason = _send_verified_posix_force(
        root,
        process_group_id,
    )
    if not force_sent:
        return TerminationResult(False, force_reason)
    if _wait_for_tree_exit(identities, force_timeout):
        return TerminationResult(True, "process_tree_terminated")
    return TerminationResult(False, "process_termination_timeout")
