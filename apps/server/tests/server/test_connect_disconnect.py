"""TASK-47 login and connection are separate; disconnect and idle sessions free their slot
(ADR-0010, ADR-0011; COM-01, NFR-03)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.server.test_admin_missions import Admin
from tests.server.test_frames import make_client, next_of, workdir  # noqa: F401
from tests.server.test_missions import FakeMono
from voa.server.app import create_app
from voa.server.settings import Settings

S = 1_000_000_000
CAPS = {"inputs": ["camera_0"], "outputs": ["hud"]}
PASSWORD = "wearer-pass-1"


@pytest.fixture
def client(workdir: Path) -> Iterator[TestClient]:  # noqa: F811
    with make_client(workdir) as c:
        yield c


def wearer_login(client: TestClient) -> dict[str, str]:
    signup = client.post("/v1/auth/signup", json={"username": "kim", "password": PASSWORD}).json()
    Admin(client).post(f"/users/{signup['user_id']}:approve")
    login = client.post("/v1/auth/login", json={"username": "kim", "password": PASSWORD}).json()
    return {"Authorization": f"Bearer {login['token']}"}


def connect(client: TestClient, login: dict[str, str], mission_id: str = "fire_c1") -> Any:
    return client.post(
        "/v1/sessions", json={"capabilities": CAPS, "mission_id": mission_id}, headers=login
    )


def test_a_wearer_lists_missions_with_the_login_and_connects_only_for_detection(
    client: TestClient,
) -> None:
    login = wearer_login(client)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    missions = client.get("/v1/missions", headers=login).json()["missions"]
    assert "fire_c1" in [m["mission_id"] for m in missions]
    assert runtime.sessions.count() == 0  # browsing missions holds no slot
    created = connect(client, login)
    assert created.status_code == 200 and created.json()["mission"]["mission_id"] == "fire_c1"
    assert runtime.sessions.count() == 1


def test_disconnect_closes_the_socket_ends_the_session_and_keeps_the_login(
    client: TestClient,
) -> None:
    login = wearer_login(client)
    session = connect(client, login).json()
    bearer = {"Authorization": f"Bearer {session['token']}"}
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    with client.websocket_connect(f"/v1/streams/{session['session_id']}", headers=bearer) as ws:
        next_of(ws, "auth_ok")
        assert client.delete(f"/v1/sessions/{session['session_id']}", headers={}).status_code == 401
        other = connect(client, login).json()
        other_bearer = {"Authorization": f"Bearer {other['token']}"}
        assert (
            client.delete(f"/v1/sessions/{session['session_id']}", headers=other_bearer).status_code
            == 404
        )
        gone = client.delete(f"/v1/sessions/{session['session_id']}", headers=bearer)
        assert gone.status_code == 204
    assert session["session_id"] not in runtime.session_started
    assert runtime.sessions.count() == 1  # only the other one
    assert client.get("/v1/missions", headers=login).status_code == 200  # still logged in
    assert client.delete(f"/v1/sessions/{session['session_id']}", headers=bearer).status_code == 401


def test_disconnecting_frees_a_slot_for_the_next_device(client: TestClient) -> None:
    login = wearer_login(client)
    sessions = [connect(client, login).json() for _ in range(3)]
    assert connect(client, login).json()["code"] == "capacity_exceeded"
    first = sessions[0]
    client.delete(
        f"/v1/sessions/{first['session_id']}", headers={"Authorization": f"Bearer {first['token']}"}
    )
    assert connect(client, login).status_code == 200


def test_sessions_without_a_control_socket_end_after_the_idle_time(workdir: Path) -> None:  # noqa: F811
    mono = FakeMono()
    env = {"VOA_HEARTBEAT_MS": "50", "VOA_CLIENT_WATCHDOG_MS": "100"}
    settings = Settings.from_env(env, base_dir=workdir)
    with TestClient(create_app(lambda: settings, mono_ns=mono)) as client:
        login = wearer_login(client)
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        portal = client.portal
        assert portal is not None

        def sweep() -> list[str]:  # on the server's thread (SQLite)
            return list(portal.call(runtime.sweep_sessions, mono.now))

        assert settings.session_idle_s == 120  # default: two minutes (owner decision)
        crashed = connect(client, login).json()  # never connects, like an app killed at once
        held = connect(client, login).json()
        with client.websocket_connect(
            f"/v1/streams/{held['session_id']}",
            headers={"Authorization": f"Bearer {held['token']}"},
        ) as ws:
            next_of(ws, "auth_ok")
            mono.now += 119 * S
            assert sweep() == []
            mono.now += 1 * S
            assert sweep() == [crashed["session_id"]]
        # the held socket closed now: idle from this moment, not from creation
        mono.now += 119 * S
        assert sweep() == []
        mono.now += 1 * S
        assert sweep() == [held["session_id"]]
        assert runtime.sessions.count() == 0


def test_a_full_server_sweeps_idle_sessions_before_refusing(workdir: Path) -> None:  # noqa: F811
    mono = FakeMono()
    settings = Settings.from_env({}, base_dir=workdir)
    with TestClient(create_app(lambda: settings, mono_ns=mono)) as client:
        login = wearer_login(client)
        for _ in range(3):
            connect(client, login)  # three crashed apps
        assert connect(client, login).json()["code"] == "capacity_exceeded"
        mono.now += 120 * S
        assert connect(client, login).status_code == 200


def test_login_tokens_that_cannot_connect(client: TestClient) -> None:
    caps = {"capabilities": CAPS}
    assert client.post("/v1/sessions", json=caps).json() == {"code": "invalid_request"}
    bad = client.post("/v1/sessions", json=caps, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert client.get("/v1/missions", headers={"Authorization": "Bearer nope"}).status_code == 401
