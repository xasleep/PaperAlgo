from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STOP_SCRIPT = REPO_ROOT / "scripts" / "stop_paperalgo.ps1"


def launch_stop_script() -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-File", str(STOP_SCRIPT), "-Quiet"],
        cwd=REPO_ROOT,
        creationflags=creationflags,
    )
