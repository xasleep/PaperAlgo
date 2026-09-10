from pathlib import Path
import subprocess


repo_root = Path(__file__).resolve().parents[1]
start_script = repo_root / "scripts" / "start_paperalgo.ps1"

subprocess.Popen(
    ["powershell.exe", "-NoProfile", "-File", str(start_script)],
    cwd=repo_root,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
