"""TASK-27 refresh token naming, session cleanup and retention purge (ADR-0005, SPEC 7.1, 8.1)."""

import shutil
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from voa.server.app import create_app
from voa.server.settings import Settings, SettingsError
from voa.storage.sqlite import SQLiteStore

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
SECRET = "example-secret-change-me"
S = 1_000_000_000
BODY = {
    "device_id": "dev_example",
    "secret": SECRET,
    "capabilities": {"inputs": ["camera_0"], "outputs": ["hud"]},
}


class FakeMono:
    def __init__(self) -> None:
        self.now = 1_000 * S

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    shutil.copytree(CONFIG / "profiles", tmp_path / "config" / "profiles")
    shutil.copytree(CONFIG / "models", tmp_path / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", tmp_path / "config" / "devices.yaml")
    return tmp_path


def client(workdir: Path, mono: FakeMono, **env: str) -> TestClient:
    settings = Settings.from_env(
        {"VOA_TOKEN_TTL_S": "60", "VOA_REFRESH_TOKEN_TTL_S": "600"}
        | {f"VOA_{k}": v for k, v in env.items()},
        base_dir=workdir,
    )
    return TestClient(create_app(lambda: settings, mono_ns=mono))


def sweep(c: TestClient, now: int) -> list[str]:
    """Run the sweep on the server's event-loop thread, which owns the SQLite connection."""
    assert c.portal is not None
    runtime = c.app.state.runtime  # type: ignore[attr-defined]
    return list(c.portal.call(runtime.sweep_sessions, now))


def ended_at(workdir: Path, session_id: str) -> Any:
    connection = sqlite3.connect(workdir / "var" / "voa.sqlite3")
    try:
        row = connection.execute(
            "SELECT ended_at_utc FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    finally:
        connection.close()
    return row[0] if row else "missing"


# --- Naming (ADR-0005) ---


def test_refresh_endpoint_and_fields(workdir: Path) -> None:
    with client(workdir, FakeMono()) as c:
        created = c.post("/v1/sessions", json=BODY).json()
        assert "refresh_token" in created and "resume_token" not in created
        sid = created["session_id"]
        refreshed = c.post(
            f"/v1/sessions/{sid}/refresh", json={"refresh_token": created["refresh_token"]}
        )
        assert refreshed.status_code == 200
        assert set(refreshed.json()) == {
            "token",
            "token_expires_at_utc",
            "refresh_token",
            "refresh_token_expires_at_utc",
        }
        old_endpoint = c.post(
            f"/v1/sessions/{sid}/resume", json={"resume_token": created["refresh_token"]}
        )
        old_field = c.post(
            f"/v1/sessions/{sid}/refresh", json={"resume_token": created["refresh_token"]}
        )
    assert old_endpoint.status_code == 404
    assert (old_field.status_code, old_field.json()) == (400, {"code": "invalid_request"})


def test_settings_use_refresh_names() -> None:
    s = Settings.from_env({}, base_dir=Path("/x"))
    assert (s.refresh_token_ttl_s, s.session_sweep_interval_s) == (86400, 60)
    with pytest.raises(SettingsError) as error:
        Settings.from_env({"VOA_SESSION_SWEEP_INTERVAL_S": "0"}, base_dir=Path("/x"))
    assert "VOA_SESSION_SWEEP_INTERVAL_S" in error.value.fields


# --- Session cleanup ---


def test_disconnected_session_is_ended_when_refresh_token_expires(workdir: Path) -> None:
    mono = FakeMono()
    with client(workdir, mono) as c:
        created = c.post("/v1/sessions", json=BODY).json()
        sid = created["session_id"]
        runtime = c.app.state.runtime  # type: ignore[attr-defined]
        tick_task = runtime.tick_tasks[sid]

        mono.now += 600 * S - 1
        assert sweep(c, mono.now) == []  # still refreshable
        mono.now += 1
        assert sweep(c, mono.now) == [sid]

        assert sid not in runtime.streams and runtime.sessions.get(sid) is None
        assert sid not in runtime.tick_tasks
        refresh = c.post(
            f"/v1/sessions/{sid}/refresh", json={"refresh_token": created["refresh_token"]}
        )
        assert refresh.status_code == 401
        bearer = {"Authorization": f"Bearer {created['token']}"}
        with pytest.raises(WebSocketDenialResponse) as denied:
            c.websocket_connect(f"/v1/streams/{sid}", headers=bearer).__enter__()
        assert denied.value.status_code == 401  # swept session: refused in the handshake
    assert tick_task.cancelled()
    assert ended_at(workdir, sid) is not None


def test_refresh_extends_the_session_lifetime(workdir: Path) -> None:
    mono = FakeMono()
    with client(workdir, mono) as c:
        created = c.post("/v1/sessions", json=BODY).json()
        sid = created["session_id"]
        mono.now += 500 * S
        c.post(f"/v1/sessions/{sid}/refresh", json={"refresh_token": created["refresh_token"]})
        mono.now += 500 * S  # 1000 s after creation, 500 s after refresh
        assert sweep(c, mono.now) == []
        assert ended_at(workdir, sid) is None


def test_connected_session_is_kept_until_it_disconnects(workdir: Path) -> None:
    mono = FakeMono()
    with client(workdir, mono) as c:
        created = c.post("/v1/sessions", json=BODY).json()
        sid = created["session_id"]
        runtime = c.app.state.runtime  # type: ignore[attr-defined]
        bearer = {"Authorization": f"Bearer {created['token']}"}
        with c.websocket_connect(f"/v1/streams/{sid}", headers=bearer) as ws:
            assert ws.receive_json()["type"] == "auth_ok"
            mono.now += 700 * S
            assert sweep(c, mono.now) == []
            assert sid in runtime.streams
        for _ in range(50):  # the server notices the disconnect asynchronously
            if sid not in runtime.connections:
                break
            time.sleep(0.01)
        assert sweep(c, mono.now) == [sid]


def test_sweep_loop_runs_on_its_interval(workdir: Path) -> None:
    mono = FakeMono()
    with client(workdir, mono, SESSION_SWEEP_INTERVAL_S="1") as c:
        sid = c.post("/v1/sessions", json=BODY).json()["session_id"]
        runtime = c.app.state.runtime  # type: ignore[attr-defined]
        mono.now += 601 * S
        for _ in range(40):
            if sid not in runtime.streams:
                break
            time.sleep(0.1)
        assert sid not in runtime.streams


# --- Retention purge (SPEC 8.1) ---


def test_old_metadata_is_purged_at_start(workdir: Path) -> None:
    db = workdir / "var" / "voa.sqlite3"
    old = datetime.now(UTC) - timedelta(days=8)
    recent = datetime.now(UTC) - timedelta(days=1)
    with SQLiteStore(db) as store:
        store.save_session(session_id="sess_old", device_id="d", started_at_utc=old)
        store.save_session(session_id="sess_new", device_id="d", started_at_utc=recent)
    with client(workdir, FakeMono()) as c:
        assert c.get("/v1/healthz").status_code == 200
    connection = sqlite3.connect(db)
    try:
        ids = {row[0] for row in connection.execute("SELECT session_id FROM sessions")}
    finally:
        connection.close()
    assert ids == {"sess_new"}
