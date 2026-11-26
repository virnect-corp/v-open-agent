"""TASK-40A accounts: fixed admin and wearer sign-up (ADR-0011; COM-01, DAT-01, DAT-02)."""

import logging
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from voa.server.app import AdminDefaultPasswordError, create_app
from voa.server.settings import Settings

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
S = 1_000_000_000
CAPS = {"inputs": ["camera_0"], "outputs": ["hud"]}
WEARER_PASSWORD = "CANARY-wearer-pass-71"
NEW_ADMIN_PASSWORD = "CANARY-admin-pass-93"


class FakeMono:
    def __init__(self) -> None:
        self.now = 10_000 * S

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    shutil.copytree(CONFIG / "profiles", tmp_path / "config" / "profiles")
    shutil.copytree(CONFIG / "models", tmp_path / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", tmp_path / "config" / "devices.yaml")
    return tmp_path


def make_client(workdir: Path, mono: FakeMono, **env: str) -> TestClient:
    settings = Settings.from_env({f"VOA_{k}": v for k, v in env.items()}, base_dir=workdir)
    return TestClient(create_app(lambda: settings, mono_ns=mono))


@pytest.fixture
def api(workdir: Path) -> Iterator[tuple[TestClient, FakeMono]]:
    mono = FakeMono()
    with make_client(workdir, mono) as client:
        yield client, mono


def login(client: TestClient, username: str, password: str) -> Any:
    return client.post("/v1/auth/login", json={"username": username, "password": password})


def admin_token(client: TestClient) -> str:
    first = login(client, "admin", "admin").json()["token"]
    changed = client.post(
        "/v1/auth/password",
        json={"current_password": "admin", "new_password": NEW_ADMIN_PASSWORD},
        headers=bearer(first),
    )
    assert changed.status_code == 200
    return str(login(client, "admin", NEW_ADMIN_PASSWORD).json()["token"])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def signup(client: TestClient, username: str = "kim_worker", **extra: Any) -> Any:
    body = {"username": username, "password": WEARER_PASSWORD, **extra}
    return client.post("/v1/auth/signup", json=body)


def approve(client: TestClient, admin: str, user_id: str) -> None:
    response = client.post(f"/v1/admin/users/{user_id}:approve", headers=bearer(admin))
    assert response.status_code == 200 and response.json()["status"] == "active"


def wearer_session(client: TestClient, username: str = "kim_worker", **extra: Any) -> Any:
    body = {"username": username, "password": WEARER_PASSWORD, "capabilities": CAPS, **extra}
    return client.post("/v1/sessions", json=body)


# --- admin ---


def test_default_admin_must_change_password_before_admin_apis(
    api: tuple[TestClient, FakeMono],
) -> None:
    client, _ = api
    first = login(client, "admin", "admin")
    assert first.status_code == 200
    assert first.json()["role"] == "admin" and first.json()["password_change_required"] is True
    token = first.json()["token"]
    assert client.get("/v1/admin/users", headers=bearer(token)).json() == {
        "code": "password_change_required"
    }
    for bad in ("short", "admin"):  # too short, unchanged
        response = client.post(
            "/v1/auth/password",
            json={"current_password": "admin", "new_password": bad},
            headers=bearer(token),
        )
        assert response.status_code == 400
    token = admin_token(client)
    users = client.get("/v1/admin/users", headers=bearer(token))
    assert users.status_code == 200
    [admin] = users.json()["users"]
    assert admin["username"] == "admin" and admin["password_change_required"] is False
    assert "password_salt_hex" not in admin and "password_digest_hex" not in admin
    assert login(client, "admin", "admin").status_code == 401  # default retired


def test_admin_apis_refuse_wearers_and_anonymous_callers(
    api: tuple[TestClient, FakeMono],
) -> None:
    client, _ = api
    admin = admin_token(client)
    approve(client, admin, signup(client).json()["user_id"])
    wearer = login(client, "kim_worker", WEARER_PASSWORD).json()["token"]
    assert client.get("/v1/admin/users").status_code == 401
    assert client.get("/v1/admin/users", headers=bearer(wearer)).status_code == 401
    assert client.get("/v1/admin/users", headers=bearer("nope")).status_code == 401


def test_network_exposure_refuses_start_with_the_default_admin_password(
    workdir: Path, tmp_path: Path
) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("c", encoding="utf-8")
    key.write_text("k", encoding="utf-8")
    exposed = {
        "BIND": "0.0.0.0",
        "ALLOW_INSECURE_DEV": "false",
        "TLS_CERT": str(cert),
        "TLS_KEY": str(key),
    }
    with pytest.raises(AdminDefaultPasswordError), make_client(workdir, FakeMono(), **exposed):
        pass
    with make_client(workdir, FakeMono()) as local:  # change it on loopback first
        admin_token(local)
    with make_client(workdir, FakeMono(), **exposed) as remote:
        assert remote.get("/v1/healthz").status_code == 200


# --- sign-up and login ---


@pytest.mark.parametrize(
    ("username", "password"),
    [("Kim", WEARER_PASSWORD), ("ab", WEARER_PASSWORD), ("kim worker", WEARER_PASSWORD),
     ("kim_worker", "short"), ("kim_worker1", "kim_worker1"), ("admin", WEARER_PASSWORD)],
)  # fmt: skip
def test_signup_rejects_bad_usernames_and_passwords(
    api: tuple[TestClient, FakeMono], username: str, password: str
) -> None:
    client, _ = api
    response = client.post("/v1/auth/signup", json={"username": username, "password": password})
    assert response.status_code == 400 and response.json() == {"code": "invalid_request"}


def test_signup_is_pending_until_approved_and_names_are_unique(
    api: tuple[TestClient, FakeMono],
) -> None:
    client, _ = api
    created = signup(client, display_name="김작업")
    assert created.status_code == 201 and created.json()["status"] == "pending"
    assert signup(client).status_code == 409 and signup(client).json()["code"] == "username_taken"
    assert login(client, "kim_worker", WEARER_PASSWORD).json() == {"code": "account_pending"}
    assert login(client, "kim_worker", "wrong-password").status_code == 401  # no hint
    approve(client, admin_token(client), created.json()["user_id"])
    ok = login(client, "kim_worker", WEARER_PASSWORD)
    assert ok.status_code == 200 and ok.json()["role"] == "wearer"


def test_signup_can_be_disabled_or_made_immediate(workdir: Path) -> None:
    with make_client(workdir, FakeMono(), SIGNUP_ENABLED="false") as client:
        assert signup(client).json() == {"code": "signup_disabled"}
    with make_client(workdir, FakeMono(), SIGNUP_REQUIRES_APPROVAL="false") as client:
        assert signup(client).json()["status"] == "active"
        assert login(client, "kim_worker", WEARER_PASSWORD).status_code == 200


def test_credential_failures_look_the_same(api: tuple[TestClient, FakeMono]) -> None:
    client, _ = api
    admin = admin_token(client)
    approve(client, admin, signup(client, "disabled_one").json()["user_id"])
    users = client.get("/v1/admin/users", headers=bearer(admin)).json()["users"]
    disabled = next(u for u in users if u["username"] == "disabled_one")
    client.post(f"/v1/admin/users/{disabled['user_id']}:disable", headers=bearer(admin))
    responses = [
        login(client, "kim_unknown", WEARER_PASSWORD),
        login(client, "admin", "wrong-password"),
        login(client, "disabled_one", WEARER_PASSWORD),
    ]
    assert {(r.status_code, r.text) for r in responses} == {(401, '{"code":"unauthorized"}')}


def test_five_failures_lock_the_username_for_five_minutes(
    api: tuple[TestClient, FakeMono],
) -> None:
    client, mono = api
    admin = admin_token(client)
    approve(client, admin, signup(client).json()["user_id"])
    for _ in range(5):
        assert login(client, "kim_worker", "wrong-password").status_code == 401
    assert login(client, "kim_worker", WEARER_PASSWORD).json() == {"code": "too_many_attempts"}
    assert wearer_session(client).status_code == 429  # sessions share the limit
    mono.now += 301 * S
    assert login(client, "kim_worker", WEARER_PASSWORD).status_code == 200


# --- wearer sessions ---


def test_wearer_creates_a_session_with_the_account(
    api: tuple[TestClient, FakeMono], workdir: Path
) -> None:
    client, _ = api
    user_id = signup(client).json()["user_id"]
    assert wearer_session(client).json() == {"code": "account_pending"}
    approve(client, admin_token(client), user_id)
    created = wearer_session(client, device_label="현장 A 글래스 1")
    assert created.status_code == 200
    sid = created.json()["session_id"]
    db = sqlite3.connect(workdir / "var" / "voa.sqlite3")
    row = db.execute("SELECT device_id, user_id FROM sessions WHERE session_id = ?", (sid,))
    assert row.fetchone() == (user_id, user_id)
    path = f"/v1/streams/{sid}"
    with client.websocket_connect(path, headers=bearer(created.json()["token"])) as ws:
        assert ws.receive_json()["type"] == "auth_ok"


def test_session_body_must_choose_one_credential_kind(api: tuple[TestClient, FakeMono]) -> None:
    client, _ = api
    both = {"device_id": "dev_example", "username": "kim_worker", "password": "x"}
    assert client.post("/v1/sessions", json={**both, "capabilities": CAPS}).status_code == 400
    assert client.post("/v1/sessions", json={"capabilities": CAPS}).status_code == 400


def test_disabling_a_wearer_ends_sessions_and_logins(
    api: tuple[TestClient, FakeMono], workdir: Path
) -> None:
    client, _ = api
    admin = admin_token(client)
    user_id = signup(client).json()["user_id"]
    approve(client, admin, user_id)
    login_token = login(client, "kim_worker", WEARER_PASSWORD).json()["token"]
    session = wearer_session(client).json()
    disabled = client.post(f"/v1/admin/users/{user_id}:disable", headers=bearer(admin))
    assert disabled.status_code == 200 and disabled.json()["status"] == "disabled"
    path = f"/v1/streams/{session['session_id']}"
    with pytest.raises(WebSocketDenialResponse) as denied:
        client.websocket_connect(path, headers=bearer(session["token"])).__enter__()
    assert denied.value.status_code == 401
    change = client.post(
        "/v1/auth/password",
        json={"current_password": WEARER_PASSWORD, "new_password": "another-pass-1"},
        headers=bearer(login_token),
    )
    assert change.status_code == 401  # login token revoked
    db = sqlite3.connect(workdir / "var" / "voa.sqlite3")
    ended = db.execute(
        "SELECT user_id, ended_at_utc FROM sessions WHERE session_id = ?", (session["session_id"],)
    ).fetchone()
    assert ended[0] == user_id and ended[1] is not None


def test_reset_password_forces_a_change_before_sessions(api: tuple[TestClient, FakeMono]) -> None:
    client, _ = api
    admin = admin_token(client)
    user_id = signup(client).json()["user_id"]
    approve(client, admin, user_id)
    reset = client.post(f"/v1/admin/users/{user_id}:reset-password", headers=bearer(admin))
    temporary = reset.json()["temporary_password"]
    assert login(client, "kim_worker", WEARER_PASSWORD).status_code == 401
    body = {"username": "kim_worker", "password": temporary, "capabilities": CAPS}
    assert client.post("/v1/sessions", json=body).json() == {"code": "password_change_required"}
    first = login(client, "kim_worker", temporary).json()
    assert first["password_change_required"] is True
    client.post(
        "/v1/auth/password",
        json={"current_password": temporary, "new_password": WEARER_PASSWORD},
        headers=bearer(first["token"]),
    )
    assert wearer_session(client).status_code == 200
    unknown = client.post("/v1/admin/users/usr_nope:approve", headers=bearer(admin))
    assert unknown.status_code == 404


# --- storage and logs ---


def test_passwords_and_usernames_never_reach_the_db_or_logs(
    api: tuple[TestClient, FakeMono], workdir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = api
    admin = admin_token(client)
    approve(client, admin, signup(client).json()["user_id"])
    wearer_session(client)
    data = (workdir / "var" / "voa.sqlite3").read_bytes()
    assert WEARER_PASSWORD.encode() not in data and NEW_ADMIN_PASSWORD.encode() not in data
    for record in caplog.records:
        text = record.getMessage() + str(record.__dict__)
        assert WEARER_PASSWORD not in text and NEW_ADMIN_PASSWORD not in text
        assert "kim_worker" not in text
