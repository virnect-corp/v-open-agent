"""TASK-45/46 `/ui/admin` and `/ui/sim` static page serving (ADR-0009, ADR-0011).

Page logic is tested with fake API responses in `tests/ui/*.test.mjs` (scripts/ui-test.sh).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.server.test_frames import make_client, workdir  # noqa: F401
from voa.server.ui import ADMIN_FILES, SIM_FILES, UI_ROOT

PAGES = [("admin", ADMIN_FILES), ("sim", SIM_FILES)]


@pytest.fixture
def client(workdir: Path) -> Iterator[TestClient]:  # noqa: F811
    with make_client(workdir) as c:
        yield c


@pytest.mark.parametrize(("folder", "files"), PAGES)
def test_pages_and_their_files_are_served_with_strict_headers(
    client: TestClient, folder: str, files: dict[str, str]
) -> None:
    page = client.get(f"/ui/{folder}")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert f'<script type="module" src="/ui/{folder}/app.js">' in page.text
    csp = page.headers["content-security-policy"]
    assert (
        "script-src 'self'" in csp
        and "frame-ancestors 'none'" in csp
        and "default-src 'none'" in csp
    )
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "media-src blob:" in csp and page.headers["permissions-policy"].startswith(
        "camera=(self)"
    )
    for name in files:
        served = client.get(f"/ui/{folder}/{name}")
        assert served.status_code == 200 and served.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/ui/{folder}/app.js").headers["content-type"].startswith("text/javascript")


@pytest.mark.parametrize("folder", ["admin", "sim"])
@pytest.mark.parametrize(
    "name", ["missing.js", "..%2Fui.py", "%2e%2e%2fapp.py", "index.HTML", "admin.css"]
)
def test_only_listed_files_are_served(client: TestClient, folder: str, name: str) -> None:
    files = ADMIN_FILES if folder == "admin" else SIM_FILES
    expected = 200 if name in files else 404
    assert client.get(f"/ui/{folder}/{name}").status_code == expected


def test_the_page_needs_no_login_to_load_but_holds_no_data(client: TestClient) -> None:
    page = client.get("/ui/admin")  # the API, not the page, is protected
    assert "token" not in page.text.lower()
    assert client.get("/v1/admin/missions").status_code == 401


@pytest.mark.parametrize(("page", "files"), PAGES)
def test_page_files_have_no_inline_script_or_external_origin(
    page: str, files: dict[str, str]
) -> None:
    folder = UI_ROOT / page
    assert sorted(p.name for p in folder.iterdir()) == sorted(files)
    html = (folder / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "style=" not in html
    for name in files:
        text = (folder / name).read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, name
        assert "·" not in text, name
