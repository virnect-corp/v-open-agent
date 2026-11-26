"""TASK-45 `/ui/admin` static page serving (ADR-0009, ADR-0011).

Page logic is tested with fake API responses in `tests/ui/*.test.mjs` (scripts/ui-test.sh).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.server.test_frames import make_client, workdir  # noqa: F401
from voa.server.ui import ADMIN_FILES, UI_ROOT


@pytest.fixture
def client(workdir: Path) -> Iterator[TestClient]:  # noqa: F811
    with make_client(workdir) as c:
        yield c


def test_the_admin_page_and_its_files_are_served_with_strict_headers(client: TestClient) -> None:
    page = client.get("/ui/admin")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert '<script type="module" src="/ui/admin/app.js">' in page.text
    csp = page.headers["content-security-policy"]
    assert (
        "script-src 'self'" in csp
        and "frame-ancestors 'none'" in csp
        and "default-src 'none'" in csp
    )
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    for name in ADMIN_FILES:
        served = client.get(f"/ui/admin/{name}")
        assert served.status_code == 200 and served.headers["x-content-type-options"] == "nosniff"
    assert client.get("/ui/admin/app.js").headers["content-type"].startswith("text/javascript")


@pytest.mark.parametrize("name", ["missing.js", "..%2Fui.py", "%2e%2e%2fapp.py", "index.HTML"])
def test_only_listed_files_are_served(client: TestClient, name: str) -> None:
    assert client.get(f"/ui/admin/{name}").status_code == 404


def test_the_page_needs_no_login_to_load_but_holds_no_data(client: TestClient) -> None:
    page = client.get("/ui/admin")  # the API, not the page, is protected
    assert "token" not in page.text.lower()
    assert client.get("/v1/admin/missions").status_code == 401


def test_page_files_have_no_inline_script_or_external_origin() -> None:
    folder = UI_ROOT / "admin"
    assert sorted(p.name for p in folder.iterdir()) == sorted(ADMIN_FILES)
    html = (folder / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "style=" not in html
    for name in ADMIN_FILES:
        text = (folder / name).read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, name
        assert "·" not in text, name
