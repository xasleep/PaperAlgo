import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from web_api import main as main_module, settings_store
from web_api.schemas import WebSettings


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"


@pytest.fixture()
def settings_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[TestClient, Path]:
    settings_path = tmp_path / "web_settings.json"
    monkeypatch.setattr(settings_store, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(settings_store, "SETTINGS_PATH", settings_path)
    client = TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    )
    session = client.get(f"{API_PREFIX}/session", headers={"Origin": LOCAL_ORIGIN})
    client.headers.update(
        {"Origin": LOCAL_ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]}
    )
    return client, settings_path


def _settings_payload(
    reproduce_key: str = "super-secret-reproduce",
    evaluation_key: str = "super-secret-eval",
) -> dict[str, Any]:
    return {
        "reproduce": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "api_key": reproduce_key,
            "base_url": "https://reproduce.invalid/v1",
        },
        "evaluation": {
            "provider": "qwen",
            "model": "qwen3.7-max",
            "api_key": evaluation_key,
            "base_url": "https://evaluation.invalid/v1",
            "fallback_models": [],
        },
    }


def _contains_key(data: object, key_name: str) -> bool:
    if isinstance(data, dict):
        return key_name in data or any(
            _contains_key(value, key_name) for value in data.values()
        )
    if isinstance(data, list):
        return any(_contains_key(item, key_name) for item in data)
    return False


def _post_job(client: TestClient):
    return client.post(
        f"{API_PREFIX}/jobs",
        json={"upload_id": "0" * 32},
    )


def test_missing_settings_file_is_treated_as_unconfigured(
    settings_client: tuple[TestClient, Path],
) -> None:
    client, settings_path = settings_client
    assert not settings_path.exists()

    status_response = client.get(f"{API_PREFIX}/settings/status")
    job_response = _post_job(client)

    assert status_response.status_code == 200
    assert status_response.json()["configured"] is False
    assert job_response.status_code == 400
    assert job_response.json()["error"]["code"] == "settings_not_configured"


def test_settings_status_treats_corrupt_settings_file_as_unconfigured(
    settings_client: tuple[TestClient, Path],
) -> None:
    client, settings_path = settings_client
    settings_path.write_text('{"reproduce": ', encoding="utf-8")

    response = client.get(f"{API_PREFIX}/settings/status")

    assert response.status_code == 200
    assert response.json()["configured"] is False


def test_create_job_returns_clear_4xx_for_corrupt_settings_file(
    settings_client: tuple[TestClient, Path],
) -> None:
    client, settings_path = settings_client
    settings_path.write_text('{"reproduce": ', encoding="utf-8")

    response = _post_job(client)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "settings_not_configured"


def test_empty_settings_file_is_treated_as_unconfigured(
    settings_client: tuple[TestClient, Path],
) -> None:
    client, settings_path = settings_client
    settings_path.write_text("", encoding="utf-8")

    status_response = client.get(f"{API_PREFIX}/settings/status")
    job_response = _post_job(client)

    assert status_response.status_code == 200
    assert status_response.json()["configured"] is False
    assert job_response.status_code == 400
    assert job_response.json()["error"]["code"] == "settings_not_configured"


def test_invalid_settings_shape_is_treated_as_unconfigured(
    settings_client: tuple[TestClient, Path],
) -> None:
    client, settings_path = settings_client
    settings_path.write_text(json.dumps({"reproduce": {}}), encoding="utf-8")

    status_response = client.get(f"{API_PREFIX}/settings/status")
    job_response = _post_job(client)

    assert status_response.status_code == 200
    assert status_response.json()["configured"] is False
    assert job_response.status_code == 400
    assert job_response.json()["error"]["code"] == "settings_not_configured"


def test_settings_atomic_write_failure_keeps_existing_file(
    settings_client: tuple[TestClient, Path],
) -> None:
    _, settings_path = settings_client

    class BrokenSettings:
        def model_dump(self) -> dict[str, object]:
            return {"bad": object()}

    settings_store.save_settings(WebSettings(**_settings_payload()))
    original_text = settings_path.read_text(encoding="utf-8")

    with pytest.raises(TypeError):
        settings_store.save_settings(BrokenSettings())  # type: ignore[arg-type]

    assert settings_path.read_text(encoding="utf-8") == original_text
    assert json.loads(original_text)["reproduce"]["api_key"] == "super-secret-reproduce"
    assert list(settings_path.parent.glob("*.tmp")) == []


def test_save_settings_hardens_local_storage_before_and_after_write(
    settings_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings_path = settings_client
    calls: list[tuple[Path, Path, bool]] = []

    def fake_harden(local_dir: Path, current_settings_path: Path) -> None:
        calls.append(
            (local_dir, current_settings_path, current_settings_path.exists())
        )

    monkeypatch.setattr(settings_store, "harden_local_storage", fake_harden)

    settings_store.save_settings(WebSettings(**_settings_payload()))

    assert calls == [
        (settings_path.parent, settings_path, False),
        (settings_path.parent, settings_path, True),
    ]


def test_load_settings_attempts_to_harden_existing_file(
    settings_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings_path = settings_client
    settings_path.write_text(json.dumps(_settings_payload()), encoding="utf-8")
    calls: list[tuple[Path, Path]] = []

    def fake_harden(local_dir: Path, current_settings_path: Path) -> None:
        calls.append((local_dir, current_settings_path))

    monkeypatch.setattr(settings_store, "harden_local_storage", fake_harden)

    loaded = settings_store.load_settings()

    assert loaded is not None
    assert calls == [(settings_path.parent, settings_path)]


def test_load_settings_tolerates_hardening_failure_for_existing_file(
    settings_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings_path = settings_client
    settings_path.write_text(json.dumps(_settings_payload()), encoding="utf-8")

    def broken_harden(local_dir: Path, current_settings_path: Path) -> None:
        raise settings_store.LocalStorageSecurityError("acl failed")

    monkeypatch.setattr(settings_store, "harden_local_storage", broken_harden)

    loaded = settings_store.load_settings()

    assert loaded is not None
    assert loaded.reproduce.api_key == "super-secret-reproduce"


def test_update_settings_returns_clear_error_when_storage_cannot_be_secured(
    settings_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = settings_client

    def broken_harden(local_dir: Path, current_settings_path: Path) -> None:
        raise settings_store.LocalStorageSecurityError("acl failed")

    monkeypatch.setattr(settings_store, "harden_local_storage", broken_harden)

    response = client.post(f"{API_PREFIX}/settings", json=_settings_payload())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["error"]["message"] == "Failed to secure local settings storage."


def test_settings_endpoints_still_do_not_echo_api_keys(
    settings_client: tuple[TestClient, Path],
) -> None:
    client, _ = settings_client
    payload = _settings_payload()

    post_response = client.post(f"{API_PREFIX}/settings", json=payload)
    status_response = client.get(f"{API_PREFIX}/settings/status")

    assert post_response.status_code == 200
    assert status_response.status_code == 200
    for response in [post_response, status_response]:
        response_text = response.text
        assert "super-secret-reproduce" not in response_text
        assert "super-secret-eval" not in response_text
        body = response.json()
        assert body["configured"] is True
        assert body["reproduce"]["has_api_key"] is True
        assert body["evaluation"]["has_api_key"] is True
        assert not _contains_key(body, "api_key")
