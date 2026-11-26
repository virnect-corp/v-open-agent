"""Static pages `/ui/admin` (TASK-45) and `/ui/sim` (TASK-46); ADR-0009, ADR-0011.

Only the files listed here are served, from the package folder, so no path from the
request reaches the file system. The page calls the same API as any client; the
admin login token stays in page memory (core.js), never in the URL, storage or
cookies. Headers forbid framing, inline scripts, other origins and caching.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

UI_ROOT = Path(__file__).resolve().parent / "ui"
ADMIN_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "core.js": "text/javascript; charset=utf-8",
    "admin.css": "text/css; charset=utf-8",
}
SIM_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "core.js": "text/javascript; charset=utf-8",
    "sim.css": "text/css; charset=utf-8",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
        "media-src blob:; connect-src 'self'; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
    # The sim page plays local video (blob:) and uses the camera; nothing else.
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _serve(folder: str, name: str, files: dict[str, str]) -> Response:
    media_type = files.get(name)
    if media_type is None:
        return JSONResponse({"code": "not_found"}, status_code=404)
    body = (UI_ROOT / folder / name).read_bytes()
    return Response(body, media_type=media_type, headers=SECURITY_HEADERS)


def register_ui_routes(app: FastAPI) -> None:
    @app.get("/ui/admin")
    async def admin_page() -> Response:
        return _serve("admin", "index.html", ADMIN_FILES)

    @app.get("/ui/admin/{name}")
    async def admin_file(name: str) -> Response:
        return _serve("admin", name, ADMIN_FILES)

    @app.get("/ui/sim")
    async def sim_page() -> Response:
        return _serve("sim", "index.html", SIM_FILES)

    @app.get("/ui/sim/{name}")
    async def sim_file(name: str) -> Response:
        return _serve("sim", name, SIM_FILES)
