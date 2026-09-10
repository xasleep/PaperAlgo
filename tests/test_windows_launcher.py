from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
START_SCRIPT = SCRIPTS / "start_paperalgo.ps1"
STOP_SCRIPT = SCRIPTS / "stop_paperalgo.ps1"
INSTALL_SCRIPT = SCRIPTS / "install_paperalgo_shortcuts.ps1"
COMMON_SCRIPT = SCRIPTS / "paperalgo_launcher_common.ps1"
START_PYW = SCRIPTS / "start_paperalgo.pyw"
PS = "powershell.exe"


def _is_windows() -> bool:
    return os.name == "nt"


pytestmark = pytest.mark.skipif(not _is_windows(), reason="Windows launcher tests require Windows.")


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _run_ps_file(script: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            PS,
            "-NoProfile",
            "-File",
            str(script),
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_ps_command(command: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PS, "-NoProfile", "-Command", command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_dist_exists() -> None:
    if not (REPO_ROOT / "web_ui" / "dist" / "index.html").is_file():
        pytest.skip("web_ui/dist/index.html is required for launcher smoke tests.")


def _listening_pids(port: int) -> set[int]:
    command = (
        f"@(Get-NetTCPConnection -LocalPort {port} -State Listen "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess) "
        "| ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [PS, "-NoProfile", "-Command", command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return set()
    data = json.loads(completed.stdout)
    if isinstance(data, int):
        return {data}
    return {int(item) for item in data}


def _launcher_dirs(tmp_path: Path, port: int) -> list[str]:
    local_dir = tmp_path / "local state"
    runs_dir = tmp_path / "runs state"
    launcher_dir = tmp_path / "launcher state"
    database_path = local_dir / "paper2code.db"
    return [
        "-Port",
        str(port),
        "-LocalDirectory",
        str(local_dir),
        "-RunsDirectory",
        str(runs_dir),
        "-DatabasePath",
        str(database_path),
        "-LauncherDirectory",
        str(launcher_dir),
    ]


def _stop_test_launcher(tmp_path: Path, port: int) -> None:
    _run_ps_file(
        STOP_SCRIPT,
        *_launcher_dirs(tmp_path, port),
        "-Quiet",
        timeout=30,
    )


def _wait_health(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/v1/health",
                timeout=2,
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
                if data.get("status") == "ok":
                    return
        except Exception as exc:  # pragma: no cover - reported in assertion below
            last_error = exc
            time.sleep(0.25)
    raise AssertionError(f"Timed out waiting for API health on {port}: {last_error}")


def test_launcher_scripts_parse_with_windows_powershell() -> None:
    for script in [COMMON_SCRIPT, START_SCRIPT, STOP_SCRIPT, INSTALL_SCRIPT]:
        command = (
            "$tokens = $null; $errors = $null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile({_ps_quote(script)}, [ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
        )
        completed = subprocess.run(
            [PS, "-NoProfile", "-Command", command],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert completed.returncode == 0, completed.stderr

    pyw_text = START_PYW.read_text(encoding="utf-8")
    assert "start_paperalgo.ps1" in pyw_text
    assert "-Execution" + "Policy" not in pyw_text
    assert "-WindowStyle" not in pyw_text


def test_launcher_scripts_keep_local_fixed_safe_defaults() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [COMMON_SCRIPT, START_SCRIPT, STOP_SCRIPT, INSTALL_SCRIPT, START_PYW]
    )
    start_text = START_SCRIPT.read_text(encoding="utf-8")
    common_text = COMMON_SCRIPT.read_text(encoding="utf-8")
    install_text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert str(REPO_ROOT) not in combined
    assert "Split-Path -Parent $PSScriptRoot" in combined
    assert "127.0.0.1" in common_text
    assert "[int]$Port = 8000" in start_text
    assert "[int]$Port = 8000" in STOP_SCRIPT.read_text(encoding="utf-8")
    assert "--reload" not in start_text
    assert "npm run dev" not in start_text
    assert "npm run build" not in start_text
    assert "Stop-Process -Name" not in combined
    assert "Get-Process python" not in combined
    assert "taskkill" not in combined.lower()
    assert "npm install" not in install_text
    assert "npm ci" not in install_text
    assert "-Execution" + "Policy" not in install_text
    assert "WindowStyle" not in install_text


def test_test_helpers_do_not_bypass_execution_policy() -> None:
    test_text = Path(__file__).read_text(encoding="utf-8")
    assert "-Execution" + "Policy" not in test_text
    assert "By" + "pass" not in test_text


def test_install_script_creates_valid_shortcuts_in_temp_directory(tmp_path: Path) -> None:
    shortcut_dir = tmp_path / "Desktop With Space"
    old_stop_link = shortcut_dir / "停止 PaperAlgo.lnk"
    shortcut_dir.mkdir()
    old_stop_link.write_text("legacy shortcut placeholder", encoding="utf-8")
    completed = _run_ps_file(
        INSTALL_SCRIPT,
        "-ShortcutDirectory",
        str(shortcut_dir),
        "-SkipBuild",
        "-Quiet",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    start_link = shortcut_dir / "启动 PaperAlgo.lnk"
    stop_link = shortcut_dir / "停止 PaperAlgo.lnk"
    assert start_link.is_file()
    assert not stop_link.exists()
    assert sorted(path.name for path in shortcut_dir.glob("*PaperAlgo.lnk")) == [
        "启动 PaperAlgo.lnk"
    ]

    command = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$a = $shell.CreateShortcut({_ps_quote(start_link)}); "
        "[pscustomobject]@{"
        "startTarget=$a.TargetPath; startArguments=$a.Arguments; startWorkingDirectory=$a.WorkingDirectory; "
        "startIcon=$a.IconLocation"
        "} | ConvertTo-Json -Compress"
    )
    inspected = subprocess.run(
        [PS, "-NoProfile", "-Command", command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert inspected.returncode == 0, inspected.stderr
    data = json.loads(inspected.stdout)

    assert Path(data["startTarget"]).name.lower() == "pythonw.exe"
    assert Path(data["startTarget"]) == REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    assert data["startWorkingDirectory"] == str(REPO_ROOT)
    assert data["startArguments"] == f'"{START_PYW}"'
    assert Path(str(data["startIcon"]).split(",", 1)[0]).name.lower() == "pythonw.exe"
    assert "powershell" not in data["startTarget"].lower()
    assert "powershell" not in data["startArguments"].lower()
    assert "ExecutionPolicy" not in data["startArguments"]
    assert "WindowStyle" not in data["startArguments"]
    assert "stop_paperalgo" not in data["startArguments"]
    assert "-SkipBuild" not in data["startArguments"]
    assert "-Quiet" not in data["startArguments"]


def test_forged_or_corrupt_runtime_does_not_stop_unrelated_process(tmp_path: Path) -> None:
    port = _free_port()
    launcher_dir = tmp_path / "launcher state"
    launcher_dir.mkdir(parents=True)
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=REPO_ROOT,
    )
    try:
        runtime = {
            "schemaVersion": 1,
            "repoRoot": str(REPO_ROOT),
            "port": port,
            "processes": [
                {
                    "role": "api",
                    "processId": sleeper.pid,
                    "processStartTimeTicks": 1,
                    "executablePath": sys.executable,
                    "commandRole": "api",
                    "repoRoot": str(REPO_ROOT),
                    "startedAtUtc": "2026-01-01T00:00:00Z",
                    "stdoutLog": str(launcher_dir / "fake-out.log"),
                    "stderrLog": str(launcher_dir / "fake-err.log"),
                    "port": port,
                }
            ],
        }
        (launcher_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
        completed = _run_ps_file(
            STOP_SCRIPT,
            *_launcher_dirs(tmp_path, port),
            "-Quiet",
            timeout=30,
        )
        assert completed.returncode != 0
        assert sleeper.poll() is None

        (launcher_dir / "runtime.json").write_text("{not-json", encoding="utf-8")
        completed = _run_ps_file(
            STOP_SCRIPT,
            *_launcher_dirs(tmp_path, port),
            "-Quiet",
            timeout=30,
        )
        assert completed.returncode == 0
        assert sleeper.poll() is None
        assert list(launcher_dir.glob("runtime.corrupt.*.json"))
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)


def test_unknown_port_occupant_causes_safe_start_failure(tmp_path: Path) -> None:
    _assert_dist_exists()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        completed = _run_ps_file(
            START_SCRIPT,
            *_launcher_dirs(tmp_path, port),
            "-NoBrowser",
            "-Quiet",
            timeout=40,
        )
        assert completed.returncode != 0
        assert listener.fileno() != -1
        assert not (tmp_path / "launcher state" / "runtime.json").exists()


def test_stop_refuses_when_api_reports_queued_job(tmp_path: Path) -> None:
    _assert_dist_exists()
    from web_api.job_repository import JobRepository

    port = _free_port()
    args = _launcher_dirs(tmp_path, port)
    database_path = tmp_path / "local state" / "paper2code.db"
    repository = JobRepository(database_path)
    repository.create_job(
        job_id="queued_job_for_launcher_stop_test",
        request={
            "upload_id": "upload",
            "paper_name": "queued",
            "domain": "statistics",
            "eval_type": "ref_free",
            "generated_n": 1,
            "auto_refine": False,
            "max_repair_rounds": 0,
            "console_output": "quiet",
            "skip_mineru": True,
            "pdf_markdown_path": "",
            "cost_budget_policy": "none",
            "cost_budget_currency": None,
            "cost_budget_amount": None,
        },
        paper_name="queued",
        provider_snapshot={
            "reproduce_provider": "openai",
            "reproduce_model": "gpt-4.1-mini",
            "evaluation_provider": "openai",
            "evaluation_model": "gpt-4.1-mini",
            "evaluation_fallback_models": [],
            "provider_registry_version": 1,
            "provider_contract_fingerprint": "a" * 64,
        },
    )

    launch_command = (
        f". {_ps_quote(COMMON_SCRIPT)}; "
        "$context = New-LauncherContext "
        f"-ScriptRoot {_ps_quote(SCRIPTS)} "
        f"-Port {port} "
        f"-LocalDirectory {_ps_quote(tmp_path / 'local state')} "
        f"-RunsDirectory {_ps_quote(tmp_path / 'runs state')} "
        f"-DatabasePath {_ps_quote(database_path)} "
        f"-LauncherDirectory {_ps_quote(tmp_path / 'launcher state')}; "
        "$previous = Set-LauncherEnvironment -Context $context; "
        "try { $record = Start-LauncherManagedProcess -Context $context -Role api } "
        "finally { Restore-LauncherEnvironment -Previous $previous }; "
        "Save-LauncherRuntime -Context $context -Processes @($record)"
    )
    cleanup_command = (
        f". {_ps_quote(COMMON_SCRIPT)}; "
        "$context = New-LauncherContext "
        f"-ScriptRoot {_ps_quote(SCRIPTS)} "
        f"-Port {port} "
        f"-LocalDirectory {_ps_quote(tmp_path / 'local state')} "
        f"-RunsDirectory {_ps_quote(tmp_path / 'runs state')} "
        f"-DatabasePath {_ps_quote(database_path)} "
        f"-LauncherDirectory {_ps_quote(tmp_path / 'launcher state')}; "
        "$runtime = Read-LauncherRuntime -Context $context; "
        "$record = Get-RuntimeRecord -Runtime $runtime -Role api; "
        "if ($null -ne $record) { Stop-VerifiedLauncherProcess -Context $context -Record $record -Role api | Out-Null }; "
        "Archive-LauncherRuntime -Context $context -Reason stopped"
    )
    try:
        launched = _run_ps_command(launch_command, timeout=30)
        assert launched.returncode == 0, launched.stdout + launched.stderr
        _wait_health(port)

        stopped = _run_ps_file(
            STOP_SCRIPT,
            *args,
            "-Quiet",
            timeout=30,
        )
        assert stopped.returncode != 0
        _wait_health(port, timeout=5.0)
    finally:
        _run_ps_command(cleanup_command, timeout=30)


def test_isolated_launcher_smoke_repeat_start_and_idle_stop(tmp_path: Path) -> None:
    _assert_dist_exists()
    before_5173 = _listening_pids(5173)
    if before_5173:
        pytest.skip("Port 5173 is already occupied before launcher smoke.")

    port = _free_port()
    args = _launcher_dirs(tmp_path, port)
    try:
        first = _run_ps_file(
            START_SCRIPT,
            *args,
            "-NoBrowser",
            "-Quiet",
            timeout=70,
        )
        assert first.returncode == 0, first.stdout + first.stderr
        runtime_path = tmp_path / "launcher state" / "runtime.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        records = {record["role"]: record for record in runtime["processes"]}
        assert set(records) == {"api", "worker"}
        assert records["api"]["port"] == port
        assert records["worker"]["port"] == port

        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
        second = _run_ps_file(
            START_SCRIPT,
            *args,
            "-NoBrowser",
            "-Quiet",
            timeout=70,
        )
        assert second.returncode == 0, second.stdout + second.stderr
        repeated = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        repeated_records = {record["role"]: record for record in repeated["processes"]}
        assert repeated_records["api"]["processId"] == records["api"]["processId"]
        assert repeated_records["worker"]["processId"] == records["worker"]["processId"]
        assert _listening_pids(5173) == set()

        stopped = _run_ps_file(
            STOP_SCRIPT,
            *args,
            "-Quiet",
            timeout=40,
        )
        assert stopped.returncode == 0
        assert not runtime_path.exists()
        assert list((tmp_path / "launcher state").glob("runtime.stopped.*.json"))
        for record in records.values():
            completed = subprocess.run(
                [PS, "-NoProfile", "-Command", f"Get-Process -Id {int(record['processId'])} -ErrorAction SilentlyContinue"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert not completed.stdout.strip()
    finally:
        _stop_test_launcher(tmp_path, port)
