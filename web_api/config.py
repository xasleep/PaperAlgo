from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CODES_DIR = REPO_ROOT / "codes"
RUNS_DIR = REPO_ROOT / "runs"
LOCAL_DIR = REPO_ROOT / ".local"
UPLOADS_DIR = LOCAL_DIR / "uploads"
SETTINGS_PATH = LOCAL_DIR / "web_settings.json"

LOCAL_DEV_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

PROVIDERS = {"deepseek", "kimi", "qwen", "claude", "openai"}
DOMAINS = {"general", "statistics"}
EVAL_TYPES = {"ref_free", "ref_based"}
CONSOLE_OUTPUT_MODES = {"progress", "full", "quiet"}
