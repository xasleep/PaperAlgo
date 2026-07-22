from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
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


def _windows_create_time_from_handle(handle: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("low", wintypes.DWORD),
            ("high", wintypes.DWORD),
        ]

    creation = FileTime()
    exit_time = FileTime()
    kernel_time = FileTime()
    user_time = FileTime()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    get_process_times.restype = wintypes.BOOL
    if not get_process_times(
        wintypes.HANDLE(handle),
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        return None
    return str((int(creation.high) << 32) | int(creation.low))


def _windows_process_create_time(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != 259:
            return None
        return _windows_create_time_from_handle(int(handle))
    finally:
        close_handle(handle)


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


def _identity_is_alive(identity: ProcessIdentity) -> bool:
    return get_process_create_time(identity.pid) == identity.create_time


def _wait_for_tree_exit(identities: list[ProcessIdentity], timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not any(_identity_is_alive(identity) for identity in identities):
            return True
        time.sleep(0.05)
    return not any(_identity_is_alive(identity) for identity in identities)


def _send_graceful(process_group_id: int) -> bool:
    try:
        if sys.platform == "win32":
            os.kill(process_group_id, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process_group_id, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _force_windows_identity(identity: ProcessIdentity) -> None:
    if not _identity_is_alive(identity):
        return
    subprocess.run(
        ["taskkill", "/PID", str(identity.pid), "/T", "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _force_process_tree(
    identities: list[ProcessIdentity],
    process_group_id: int,
) -> None:
    root = identities[0]
    if sys.platform == "win32":
        _force_windows_identity(root)
        for identity in reversed(identities[1:]):
            _force_windows_identity(identity)
        return
    if _identity_is_alive(root):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for identity in reversed(identities[1:]):
        if not _identity_is_alive(identity):
            continue
        try:
            os.kill(identity.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def terminate_verified_process_tree(
    *,
    pid: int,
    expected_create_time: str,
    process_group_id: int,
    graceful_timeout: float = 10.0,
    force_timeout: float = 5.0,
) -> TerminationResult:
    status = process_identity_status(pid, expected_create_time)
    if status == "mismatch":
        return TerminationResult(False, "process_identity_mismatch")
    if status == "missing":
        return TerminationResult(False, "process_missing")

    root = ProcessIdentity(pid=pid, create_time=expected_create_time)
    identities = _snapshot_process_tree(root)
    if process_identity_status(pid, expected_create_time) != "matching":
        return TerminationResult(False, "process_identity_mismatch")

    graceful_sent = _send_graceful(process_group_id)
    if graceful_sent and _wait_for_tree_exit(identities, graceful_timeout):
        return TerminationResult(True, "process_tree_terminated")

    _force_process_tree(identities, process_group_id)
    if _wait_for_tree_exit(identities, force_timeout):
        return TerminationResult(True, "process_tree_terminated")
    return TerminationResult(False, "process_tree_termination_failed")
