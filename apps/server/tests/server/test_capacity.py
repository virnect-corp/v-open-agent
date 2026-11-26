"""TASK-43 session limit and load control (ADR-0010; NFR-03, COM-01)."""

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.server.test_frames import Session, factory, jpeg, make_client, next_of, workdir  # noqa: F401
from voa.server import frames as frames_module
from voa.server.frames import NS_PER_S, FrameState

S = NS_PER_S


@pytest.fixture
def client(workdir: Path) -> Iterator[TestClient]:  # noqa: F811
    with make_client(workdir) as c:
        yield c


# --- session limit ---


def test_the_fourth_session_is_refused_with_retry_after(client: TestClient) -> None:
    sessions = [Session(client) for _ in range(3)]
    body = {"device_id": "dev_example", "secret": "example-secret-change-me",
            "capabilities": {"inputs": ["camera_0"], "outputs": ["hud"]}}  # fmt: skip
    refused = client.post("/v1/sessions", json=body)
    assert refused.status_code == 503 and refused.headers["Retry-After"] == "30"
    assert refused.json() == {
        "code": "capacity_exceeded",
        "active": 3,
        "max": 3,
        "retry_after_s": 30,
    }
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    portal = client.portal  # the server's own thread (SQLite)
    assert portal is not None
    portal.call(runtime.end_session, sessions[0].id, runtime.mono_ns())  # e.g. swept (ADR-0005)
    assert client.post("/v1/sessions", json=body).status_code == 200


def test_a_control_socket_over_the_limit_is_told_why_then_closed_4503(client: TestClient) -> None:
    s = Session(client)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    for i in range(3):  # three other sessions already hold connections
        runtime.connections[f"sess_other_{i}"] = object()
    try:
        with s.connect() as ws:
            error = ws.receive_json()
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
    finally:
        for i in range(3):
            runtime.connections.pop(f"sess_other_{i}", None)
    assert error["type"] == "error" and error["code"] == "capacity_exceeded"
    assert (error["active"], error["max"], error["retry_after_s"]) == (1, 3, 30)
    assert closed.value.code == 4503


# --- fairness across sessions ---


def test_a_busy_session_does_not_starve_another_on_the_same_model(client: TestClient) -> None:
    a, b = Session(client), Session(client)
    with a.connect() as wa, b.connect() as wb:
        next_of(wa, "auth_ok")
        next_of(wb, "auth_ok")
        stop = threading.Event()

        def hammer() -> None:
            while not stop.is_set():
                a.upload(jpeg(b"SLOW"))

        worker = threading.Thread(target=hammer)
        worker.start()
        time.sleep(0.2)
        begin = time.monotonic()
        response = b.upload(jpeg(b"SLOW"))
        waited = time.monotonic() - begin
        stop.set()
        worker.join()
    assert response.status_code in (200, 202)
    assert waited < 1.2  # its turn comes after at most one of A's 0.4 s frames


# --- adaptive frame interval ---


def test_sustained_queueing_raises_the_interval_and_calm_restores_it() -> None:
    state = FrameState(base_interval_ms=333, interval_ms=333)
    assert state.adapt(500.0, 0, 1000) is None  # overload starts
    assert state.adapt(500.0, 4 * S, 1000) is None  # not yet 5 s
    assert state.adapt(500.0, 5 * S, 1000) == 499  # x1.5
    for t in range(6, 30):
        changed = state.adapt(900.0, t * S, 1000)
        if changed is not None:
            assert changed <= 1000
    assert state.interval_ms == 1000  # capped
    calm = [state.adapt(10.0, t * S, 1000) for t in range(40, 52)]
    assert 333 in calm and state.interval_ms == 333  # back to the base after 10 s calm


def test_a_short_spike_changes_nothing() -> None:
    state = FrameState(base_interval_ms=333, interval_ms=333)
    assert state.adapt(900.0, 0, 1000) is None
    assert state.adapt(100.0, 7 * S, 1000) is None  # window moved on: average is calm
    assert state.adapt(900.0, 8 * S, 1000) is None and state.interval_ms == 333


def test_an_interval_change_is_sent_as_config(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FrameState, "adapt", lambda self, q, n, m: 500)
    s = Session(client)
    with s.connect() as ws:
        next_of(ws, "auth_ok")
        assert s.upload(jpeg()).status_code == 200
        assert next_of(ws, "config") == {"type": "config", "frame_interval_ms": 500}


def test_capacity_report_for_admins(client: TestClient) -> None:
    first = client.post("/v1/auth/login", json={"username": "admin", "password": "admin"}).json()
    client.post(
        "/v1/auth/password",
        json={"current_password": "admin", "new_password": "admin-pass-1"},
        headers={"Authorization": f"Bearer {first['token']}"},
    )
    token = client.post(
        "/v1/auth/login", json={"username": "admin", "password": "admin-pass-1"}
    ).json()["token"]
    s = Session(client)
    with s.connect() as ws:
        next_of(ws, "auth_ok")
        s.upload(jpeg(b"OBSTACLE"))
        report: Any = client.get(
            "/v1/admin/capacity", headers={"Authorization": f"Bearer {token}"}
        ).json()
    assert (report["sessions"], report["max_sessions"]) == (1, 3)
    assert report["inferences_last_minute"] == {"mock-detector": 1}
    [detail] = report["session_details"]
    assert detail["connected"] and detail["frame_interval_ms"] == 333
    assert client.get("/v1/admin/capacity").status_code == 401


def test_frames_module_constants_match_adr() -> None:
    assert frames_module.RAISE_AFTER_NS == 5 * S and frames_module.RESTORE_AFTER_NS == 10 * S
