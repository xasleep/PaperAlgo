from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_repo_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_runtime_state_paths_can_be_redirected_for_clean_checkout(tmp_path: Path) -> None:
    local_dir = tmp_path / "local-state"
    runs_dir = tmp_path / "run-state"
    code = (
        "import json;"
        "from web_api import config;"
        "print(json.dumps({"
        "'local_dir': str(config.LOCAL_DIR),"
        "'settings_path': str(config.SETTINGS_PATH),"
        "'uploads_dir': str(config.UPLOADS_DIR),"
        "'runs_dir': str(config.RUNS_DIR),"
        "'db_path': str(config.configured_database_path()),"
        "}, sort_keys=True))"
    )
    env = {
        **os.environ,
        "PAPER2CODE_LOCAL_DIR": str(local_dir),
        "PAPER2CODE_RUNS_DIR": str(runs_dir),
        "PYTHONPATH": str(REPO_ROOT),
    }

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    paths = json.loads(completed.stdout)
    assert Path(paths["local_dir"]) == local_dir
    assert Path(paths["settings_path"]) == local_dir / "web_settings.json"
    assert Path(paths["uploads_dir"]) == local_dir / "uploads"
    assert Path(paths["runs_dir"]) == runs_dir
    assert Path(paths["db_path"]) == local_dir / "paper2code.db"


def test_dependency_files_split_runtime_dev_and_optional_heavy() -> None:
    runtime = _read_repo_text("requirements-runtime.txt")
    dev = _read_repo_text("requirements-dev.txt")
    optional = _read_repo_text("requirements-optional-heavy.txt")
    constraints = _read_repo_text("constraints.txt")
    legacy = _read_repo_text("requirements.txt")

    assert "vllm" not in runtime.lower()
    assert "pytest" not in runtime.lower()
    assert "httpx" not in runtime.lower()
    assert "vllm" in optional.lower()
    assert "-r requirements-runtime.txt" in dev
    assert "-c constraints.txt" in dev
    assert "pytest" in dev.lower()
    assert "httpx" in dev.lower()
    assert "-r requirements-runtime.txt" in legacy
    assert "-r requirements-optional-heavy.txt" in legacy

    pinned = {
        line.split("==", 1)[0].lower()
        for line in constraints.splitlines()
        if line and not line.startswith("#") and "==" in line
    }
    for package_name in {
        "openai",
        "fastapi",
        "uvicorn",
        "python-multipart",
        "pydantic",
        "starlette",
        "tiktoken",
        "transformers",
        "pytest",
        "httpx",
    }:
        assert package_name in pinned


def test_github_actions_release_ci_is_least_privilege_and_reproducible() -> None:
    workflow = _read_repo_text(".github/workflows/ci.yml")

    assert "pull_request_target" not in workflow
    assert re.search(r"(?m)^permissions:\s*\n\s+contents:\s+read\s*$", workflow)
    assert "write-all" not in workflow
    assert "runs-on: windows-latest" in workflow
    assert "npm.cmd run e2e:fake" in workflow
    assert "python -m pytest tests -q -rs" in workflow
    assert "npm.cmd run typecheck" in workflow
    assert "npm.cmd run build" in workflow
    assert "npm.cmd run verify:same-origin" in workflow
    assert "npm.cmd run smoke" in workflow
    assert "npm.cmd run smoke:prod" in workflow
    assert "hashFiles('requirements-runtime.txt', 'requirements-dev.txt', 'constraints.txt')" in workflow
    assert "hashFiles('web_ui/package-lock.json')" in workflow
    assert "pip install -r requirements-dev.txt" in workflow
    assert "pip install `\n            \"openai==" not in workflow
    assert "upload-artifact" not in workflow
    assert workflow.index("npm.cmd run build") < workflow.index("python -m pytest tests -q -rs")


def test_gitignore_excludes_release_outputs_databases_and_credentials() -> None:
    gitignore = _read_repo_text(".gitignore")
    required_patterns = {
        ".local/",
        "runs/",
        "outputs/",
        "results/",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
        "*.db-wal",
        "*.db-shm",
        "*.sqlite-wal",
        "*.sqlite-shm",
        ".pytest_cache/",
        ".pytest_tmp*/",
        "coverage/",
        "web_ui/coverage/",
        "dist/",
        "web_ui/dist/",
        "playwright-report/",
        "test-results/",
        ".playwright/",
        ".playwright-mcp/",
        ".env",
        ".env.*",
        "api.txt",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
    }
    present = {line.strip() for line in gitignore.splitlines() if line.strip()}
    assert required_patterns <= present
