from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web_api.static_ui import install_static_ui


def _write_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><div id="root"></div><script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('paper2code smoke');", encoding="utf-8")
    return dist


def test_static_ui_serves_root_fallback_and_assets(tmp_path: Path) -> None:
    dist = _write_dist(tmp_path)
    app = FastAPI()
    installed = install_static_ui(app, dist)
    client = TestClient(app)

    root = client.get("/", headers={"accept": "text/html"})
    settings = client.get("/settings", headers={"accept": "text/html"})
    asset = client.get("/assets/app.js")

    assert installed is True
    assert root.status_code == 200
    assert settings.status_code == 200
    assert '<div id="root"></div>' in settings.text
    assert asset.status_code == 200
    assert "paper2code smoke" in asset.text


def test_static_ui_does_not_swallow_api_unknown_paths(tmp_path: Path) -> None:
    dist = _write_dist(tmp_path)
    app = FastAPI()
    install_static_ui(app, dist)
    client = TestClient(app)

    response = client.get("/api/v1/missing", headers={"accept": "text/html"})

    assert response.status_code == 404
    assert '<div id="root"></div>' not in response.text


def test_spa_jobs_is_independent_of_accept_and_api_remains_json(
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    app = FastAPI()

    @app.get("/api/v1/jobs")
    def jobs() -> dict[str, list]:
        return {"jobs": []}

    install_static_ui(app, dist)
    client = TestClient(app)

    browser_response = client.get("/jobs", headers={"accept": "text/html"})
    alternate_accept = client.get("/jobs", headers={"accept": "application/json"})
    api_response = client.get("/api/v1/jobs", headers={"accept": "text/html"})

    assert browser_response.status_code == 200
    assert '<div id="root"></div>' in browser_response.text
    assert alternate_accept.status_code == 200
    assert '<div id="root"></div>' in alternate_accept.text
    assert api_response.status_code == 200
    assert api_response.json() == {"jobs": []}


def test_settings_ui_uses_registry_discovery_without_provider_or_model_allowlists() -> None:
    web_ui = Path(__file__).resolve().parents[1] / "web_ui" / "src"
    settings_source = (web_ui / "pages" / "SettingsPage.tsx").read_text(
        encoding="utf-8"
    )
    types_source = (web_ui / "api" / "types.ts").read_text(encoding="utf-8")
    client_source = (web_ui / "api" / "client.ts").read_text(encoding="utf-8")

    assert "const PROVIDERS" not in settings_source
    assert "getProviders" in settings_source
    assert "getSettings" in settings_source
    assert "getSettings" in client_source
    assert "getProviders" in client_source
    assert "ProviderRegistryResponse" in types_source
    assert "ProviderName = string" in types_source
    for hard_coded_provider in ('"openai" |', '"deepseek" |', '"claude"'):
        assert hard_coded_provider not in types_source
