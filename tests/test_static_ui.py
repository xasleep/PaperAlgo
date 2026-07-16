from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.responses import FileResponse

from web_api.static_ui import install_static_ui, spa_index_response


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


def test_static_ui_does_not_swallow_api_like_unknown_paths(tmp_path: Path) -> None:
    dist = _write_dist(tmp_path)
    app = FastAPI()
    install_static_ui(app, dist)
    client = TestClient(app)

    response = client.get("/jobs/missing/extra", headers={"accept": "text/html"})

    assert response.status_code == 404
    assert '<div id="root"></div>' not in response.text


def test_spa_response_allows_html_browser_routes_without_hiding_json_api(
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    app = FastAPI()

    @app.get("/jobs", response_model=None)
    def jobs(request: Request) -> dict[str, list] | FileResponse:
        ui_response = spa_index_response(request, dist)
        if ui_response is not None:
            return ui_response
        return {"jobs": []}

    client = TestClient(app)

    browser_response = client.get("/jobs", headers={"accept": "text/html"})
    api_response = client.get("/jobs", headers={"accept": "application/json"})

    assert browser_response.status_code == 200
    assert '<div id="root"></div>' in browser_response.text
    assert api_response.status_code == 200
    assert api_response.json() == {"jobs": []}
