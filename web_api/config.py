import os
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CODES_DIR = REPO_ROOT / "codes"
API_PREFIX = "/api/v1"
MAX_PDF_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_MULTIPART_OVERHEAD_BYTES = 1 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = MAX_PDF_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD_BYTES

LOCAL_DEV_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

_TRUSTED_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\])$")


def configured_trusted_hosts() -> list[str]:
    """Return exact local hosts plus explicitly configured host names."""

    configured = []
    for value in os.environ.get("PAPER2CODE_TRUSTED_HOSTS", "").split(","):
        host = value.strip().lower()
        if not host:
            continue
        if "*" in host or not _TRUSTED_HOST_RE.fullmatch(host):
            raise ValueError(
                "PAPER2CODE_TRUSTED_HOSTS must contain exact host names without ports or wildcards."
            )
        configured.append(host)
    return list(dict.fromkeys(["localhost", "127.0.0.1", "[::1]", *configured]))


TRUSTED_HOSTS = configured_trusted_hosts()


def _configured_path(env_name: str, default: Path) -> Path:
    configured = os.environ.get(env_name, "").strip()
    return Path(configured).expanduser() if configured else default


def configured_local_dir() -> Path:
    return _configured_path("PAPER2CODE_LOCAL_DIR", REPO_ROOT / ".local")


def configured_runs_dir() -> Path:
    return _configured_path("PAPER2CODE_RUNS_DIR", REPO_ROOT / "runs")


RUNS_DIR = configured_runs_dir()
LOCAL_DIR = configured_local_dir()
UPLOADS_DIR = LOCAL_DIR / "uploads"
SETTINGS_PATH = LOCAL_DIR / "web_settings.json"
DEFAULT_DB_PATH = LOCAL_DIR / "paper2code.db"


def configured_database_path() -> Path:
    configured = os.environ.get("PAPER2CODE_DB_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_DB_PATH


def configured_job_runtime() -> str:
    runtime = os.environ.get("JOB_RUNTIME", "legacy").strip().lower()
    if runtime not in {"legacy", "sqlite"}:
        raise ValueError("JOB_RUNTIME must be either 'legacy' or 'sqlite'.")
    return runtime


PROVIDERS = {"deepseek", "kimi", "qwen", "claude", "openai"}
DOMAINS = {"general", "statistics"}
EVAL_TYPES = {"ref_free", "ref_based"}
CONSOLE_OUTPUT_MODES = {"progress", "full", "quiet"}
