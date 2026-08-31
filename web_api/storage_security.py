import getpass
import os
import subprocess
from pathlib import Path


WINDOWS_SYSTEM_SID = "*S-1-5-18"
WINDOWS_REMOVED_GROUP_SIDS = (
    "*S-1-1-0",  # Everyone
    "*S-1-5-11",  # Authenticated Users
    "*S-1-5-32-545",  # Users
)


class LocalStorageSecurityError(RuntimeError):
    pass


def harden_local_storage(local_dir: Path, settings_path: Path) -> None:
    _apply_private_directory_permissions(local_dir)
    if settings_path.exists():
        _apply_private_file_permissions(settings_path)


def _apply_private_directory_permissions(path: Path) -> None:
    if os.name == "nt":
        _apply_windows_acl(path, permission="(OI)(CI)(F)")
        return
    path.chmod(0o700)


def _apply_private_file_permissions(path: Path) -> None:
    if os.name == "nt":
        _apply_windows_acl(path, permission="(F)")
        return
    path.chmod(0o600)


def _current_windows_principal() -> str:
    if os.name == "nt":
        completed = subprocess.run(
            ["whoami"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        principal = (completed.stdout or "").strip()
        if completed.returncode == 0 and principal:
            return principal

    username = (os.environ.get("USERNAME") or getpass.getuser() or "").strip()
    if not username:
        raise LocalStorageSecurityError(
            "Failed to determine the current Windows user for local settings ACLs."
        )

    domain = (os.environ.get("USERDOMAIN") or "").strip()
    if domain and "\\" not in username:
        return f"{domain}\\{username}"
    return username


def _apply_windows_acl(path: Path, *, permission: str) -> None:
    principal = _current_windows_principal()
    cmd = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{principal}:{permission}",
        "/grant:r",
        f"{WINDOWS_SYSTEM_SID}:{permission}",
    ]
    for sid in WINDOWS_REMOVED_GROUP_SIDS:
        cmd.extend(["/remove:g", sid])

    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode == 0:
        return

    stderr = (completed.stderr or completed.stdout or "").strip()
    detail = f" {stderr}" if stderr else ""
    raise LocalStorageSecurityError(
        f"Failed to restrict local settings permissions for {path}.{detail}"
    )
