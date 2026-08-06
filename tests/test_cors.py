from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_api import main as main_module, settings_store


ALLOWED_ORIGIN = "http://localhost:5173"
EVIL_ORIGIN = "http://evil.example"
API_PREFIX = "/api/v1"


def _settings_payload() -> dict[str, object]:
    return {
        "reproduce": {
            "provider": "deepseek",
            "model": "deepseek-test",
            "api_key": "super-secret-reproduce",
            "base_url": "",
        },
        "evaluation": {
            "provider": "qwen",
            "model": "qwen-test",
            "api_key": "super-secret-eval",
            "base_url": "",
            "fallback_models": [],
        },
    }


def _contains_key(data: object, key_name: str) -> bool:
    if isinstance(data, dict):
        return key_name in data or any(_contains_key(value, key_name) for value in data.values())
    if isinstance(data, list):
        return any(_contains_key(item, key_name) for item in data)
    return False


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    settings_path = tmp_path / ".local" / "web_settings.json"
    monkeypatch.setattr(settings_store, "LOCAL_DIR", settings_path.parent)
    monkeypatch.setattr(settings_store, "SETTINGS_PATH", settings_path)
    return TestClient(main_module.app, base_url="http://localhost")


def test_settings_preflight_allows_local_vite_origin(client: TestClient) -> None:
    response = client.options(
        f"{API_PREFIX}/settings",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,idempotency-key,x-csrf-token",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "idempotency-key" in response.headers["access-control-allow-headers"].lower()


def test_settings_preflight_does_not_allow_public_origin(client: TestClient) -> None:
    response = client.options(
        f"{API_PREFIX}/settings",
        headers={
            "Origin": EVIL_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        },
    )

    assert response.headers.get("access-control-allow-origin") is None


def test_settings_requests_from_local_vite_origin_include_cors_headers(
    client: TestClient,
) -> None:
    session = client.get(f"{API_PREFIX}/session", headers={"Origin": ALLOWED_ORIGIN})
    token = session.json()["csrf_token"]
    post_response = client.post(
        f"{API_PREFIX}/settings",
        json=_settings_payload(),
        headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": token},
    )
    status_response = client.get(
        f"{API_PREFIX}/settings/status",
        headers={"Origin": ALLOWED_ORIGIN},
    )

    assert post_response.status_code == 200
    assert status_response.status_code == 200
    for response in [post_response, status_response]:
        assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        response_text = response.text
        assert "super-secret-reproduce" not in response_text
        assert "super-secret-eval" not in response_text
        body = response.json()
        assert body["reproduce"] == {"has_api_key": True}
        assert body["evaluation"] == {"has_api_key": True}
        assert not _contains_key(body, "api_key")
