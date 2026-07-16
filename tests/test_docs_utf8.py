import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"
CHANGE_LOG_DIR = DOCS_ROOT / "prompt" / "change_logs"


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
