import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"
CHANGE_LOG_DIR = DOCS_ROOT / "prompt" / "change_logs"
ARCHITECTURE_DOCS = (
    REPO_ROOT / "README.md",
    DOCS_ROOT / "adr" / "0002-sqlite-single-worker.md",
    DOCS_ROOT / "adr" / "0004-subprocess-pipeline-adapter.md",
    DOCS_ROOT / "engineering" / "baseline.md",
    DOCS_ROOT / "info" / "全仓目录结构与模块说明.md",
    REPO_ROOT / "web_ui" / "README.md",
)


def test_change_logs_are_utf8_readable() -> None:
    change_logs = sorted(CHANGE_LOG_DIR.glob("*.md"))

    assert change_logs, "expected change log markdown files"
    for path in change_logs:
        path.read_text(encoding="utf-8")


def test_change_logs_do_not_contain_private_use_mojibake() -> None:
    for path in sorted(CHANGE_LOG_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")

        assert not re.search(r"[\ue000-\uf8ff]", text), path


def test_docs_do_not_reference_legacy_change_log_path() -> None:
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        text = path.read_text(encoding="utf-8")

        assert "docs/change_logs" not in text, path
        assert "docs\\change_logs" not in text, path


def test_pr_04a_architecture_docs_match_runtime_contract() -> None:
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    subprocess_adr = (DOCS_ROOT / "adr" / "0004-subprocess-pipeline-adapter.md").read_text(
        encoding="utf-8"
    )
    repository_guide = (
        DOCS_ROOT / "info" / "全仓目录结构与模块说明.md"
    ).read_text(encoding="utf-8")
    webui_readme = (REPO_ROOT / "web_ui" / "README.md").read_text(encoding="utf-8")

    assert "默认 `JOB_RUNTIME=legacy`" in root_readme
    assert "worker_id + instance_token" in root_readme
    assert "python -m web_api.worker" in root_readme
    assert "阶段 checkpoint" in root_readme

    assert "`JOB_RUNTIME=legacy`" in subprocess_adr
    assert "`JOB_RUNTIME=sqlite`" in subprocess_adr
    assert "API 重启不会终止" in subprocess_adr
    assert "进程级 reconciliation" in subprocess_adr

    assert ".local/uploads/<job_id>/" not in repository_guide
    assert ".local/uploads/<upload_id>/" in repository_guide
    assert "00_worker_pipeline.log" in repository_guide
    assert "API 重启" in repository_guide
    assert "Worker 重启" in repository_guide

    assert "/api/v1" in webui_readme
    assert "2 seconds" in webui_readme
    assert "SSE" in webui_readme
    assert "asynchronous" in webui_readme
    assert "does not return API key" in webui_readme


def test_architecture_docs_are_utf8_without_bom_or_private_paths() -> None:
    absolute_path = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")

    for path in ARCHITECTURE_DOCS:
        raw = path.read_bytes()
        text = raw.decode("utf-8")

        assert not raw.startswith(b"\xef\xbb\xbf"), path
        assert not absolute_path.search(text), path
        assert not re.search(r"\.(?:md|py|tsx|ts|js)\.(?:md|py|tsx|ts|js)\b", text), path


def test_architecture_markdown_relative_links_resolve() -> None:
    markdown_link = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")

    for path in ARCHITECTURE_DOCS:
        text = path.read_text(encoding="utf-8")
        for raw_target in markdown_link.findall(text):
            target = raw_target.strip().split("#", 1)[0]
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue

            resolved = (path.parent / target).resolve()
            assert resolved.exists(), f"broken relative link in {path}: {raw_target}"
