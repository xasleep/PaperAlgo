import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_UI_DIST = REPO_ROOT / "web_ui" / "dist"
WEB_UI_INDEX = WEB_UI_DIST / "index.html"
API_PREFIX = "api"
HASHED_ASSET_RE = re.compile(r"(?:^|[-.])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")


def static_ui_available(dist_dir: Path = WEB_UI_DIST) -> bool:
    return (dist_dir / "index.html").is_file()


def spa_index_response(
    request: Request,
    dist_dir: Path = WEB_UI_DIST,
) -> FileResponse | None:
    if request.method != "GET" or not static_ui_available(dist_dir):
        return None
    return FileResponse(
        str(dist_dir / "index.html"),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


def is_api_like_path(path: str) -> bool:
    first_segment = path.strip("/").split("/", 1)[0]
    return first_segment == API_PREFIX


class CacheControlledStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200 and HASHED_ASSET_RE.search(Path(path).name):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def install_static_ui(app: FastAPI, dist_dir: Path = WEB_UI_DIST) -> bool:
    if not static_ui_available(dist_dir):
        return False

    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/assets",
            CacheControlledStaticFiles(directory=str(assets_dir)),
            name="web_ui_assets",
        )

    @app.get("/", include_in_schema=False)
    def web_ui_root(request: Request) -> FileResponse:
        response = spa_index_response(request, dist_dir)
        if response is None:
            raise StarletteHTTPException(status_code=404)
        return response

    @app.get("/{full_path:path}", include_in_schema=False)
    def web_ui_fallback(request: Request, full_path: str) -> FileResponse:
        if is_api_like_path(full_path):
            raise StarletteHTTPException(status_code=404)
        response = spa_index_response(request, dist_dir)
        if response is None:
            raise StarletteHTTPException(status_code=404)
        return response

    return True
