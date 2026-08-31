from typing import Any

from pydantic import ValidationError

from .config import LOCAL_DIR, SETTINGS_PATH
from .json_io import read_json_file, write_json_file_atomic
from .schemas import (
    EvaluationSettingsStatus,
    ProviderSettingsStatus,
    SettingsStatus,
    WebSettings,
)
from .storage_security import LocalStorageSecurityError, harden_local_storage


def _model_dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def save_settings(settings: WebSettings) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    harden_local_storage(LOCAL_DIR, SETTINGS_PATH)
    write_json_file_atomic(SETTINGS_PATH, _model_dump(settings))
    harden_local_storage(LOCAL_DIR, SETTINGS_PATH)


def load_settings() -> WebSettings | None:
    try:
        if not SETTINGS_PATH.exists():
            return None
    except OSError:
        return None
    try:
        harden_local_storage(LOCAL_DIR, SETTINGS_PATH)
    except (LocalStorageSecurityError, OSError):
        pass
    try:
        data = read_json_file(SETTINGS_PATH, default={})
    except OSError:
        return None
    try:
        return WebSettings(**data)
    except (TypeError, ValidationError):
        return None


def get_settings_status() -> SettingsStatus:
    settings = load_settings()
    if settings is None:
        return SettingsStatus(configured=False)

    return SettingsStatus(
        configured=True,
        reproduce=ProviderSettingsStatus(
            has_api_key=bool(settings.reproduce.api_key),
        ),
        evaluation=EvaluationSettingsStatus(
            has_api_key=bool(settings.evaluation.api_key),
        ),
    )
