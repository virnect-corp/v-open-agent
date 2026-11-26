"""TASK-18 WebSocket stream <-> engine (SPEC 7.2, 7.4; COM-02, DEV-01, T-R17).

TASK-40: authentication moved into the handshake (ADR-0009).
"""

import contextlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from voa.server.app import create_app
from voa.server.settings import Settings

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
SECRET = "example-secret-change-me"
MS = 1_000_000
OBSTACLE = "visible_obstacle_in_corridor"


class FakeMono:
    def __init__(self) -> None:
        self.now = 10_000 * MS

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    shutil.copytree(CONFIG / "profiles", tmp_path / "config" / "profiles")
    shutil.copytree(CONFIG / "models", tmp_path / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", tmp_path / "config" / "devices.yaml")
    return tmp_path


class Harness:
    def __init__(self, client: TestClient, mono: FakeMono) -> None:
        self.client = client
        self.mono = mono
        self.seq = 0
        self.open: list[WebSocketTestSession] = []

    def session(self, inputs: list[str] | None = None) -> dict[str, Any]:
        response = self.client.post(
            "/v1/sessions",
            json={
                "device_id": "dev_example",
                "secret": SECRET,
                "capabilities": {
                    "inputs": ["camera_0"] if inputs is None else inputs,
                    "outputs": ["hud"],
                },
            },
        )
        assert response.status_code == 200
        return dict(response.json())

    def connect(
        self, session: dict[str, Any], token: str | None = None, **kwargs: Any
    ) -> WebSocketTestSession:
        headers = {"Authorization": f"Bearer {token or session['token']}"}
        return self.client.websocket_connect(
            f"/v1/streams/{session['session_id']}", headers=headers, **kwargs
        )

    def obstacle(self, *, present: bool = True, stream: str = "camera_0") -> dict[str, Any]:
        self.seq += 1
        detections = (
            [{"label": "obstacle", "bbox": [0.3, 0.5, 0.6, 0.9], "score": 0.9}] if present else []
        )
        return {
            "type": "observation",
            "observation": {
                "stream_id": stream,
                "seq": self.seq,
                "capture_ts_monotonic_ns": self.mono.now,
                "model": {"id": "mock-detector", "revision": "0"},
                "payload": {
                    "kind": "object_detection",
                    "frame_id": f"f{self.seq}",
                    "detections": detections,
                },
            },
        }


@pytest.fixture
def harness(workdir: Path) -> Iterator[Harness]:
    mono = FakeMono()
    settings = Settings.from_env(
        {"VOA_TICK_INTERVAL_MS": "10", "VOA_HEARTBEAT_MS": "50", "VOA_CLIENT_WATCHDOG_MS": "100"},
        base_dir=workdir,
    )
    with TestClient(create_app(lambda: settings, mono_ns=mono)) as client:
        h = Harness(client, mono)
        yield h
        for ws in h.open:  # sessions opened by authed() must be exited before shutdown
            with contextlib.suppress(Exception):
                ws.__exit__(None, None, None)


def receive(ws: WebSocketTestSession, *, skip: tuple[str, ...] = ("heartbeat",)) -> dict[str, Any]:
    while True:
        message = dict(ws.receive_json())
        if message["type"] not in skip:
            return message


def authed(h: Harness, session: dict[str, Any]) -> WebSocketTestSession:
    ws = h.connect(session).__enter__()
    h.open.append(ws)
    ok = receive(ws)
    assert ok["type"] == "auth_ok" and ok["session_id"] == session["session_id"]
    return ws


def closed_with(ws: WebSocketTestSession, code: int) -> None:
    with pytest.raises(WebSocketDisconnect) as error:
        while True:
            ws.receive_json()
    assert error.value.code == code


# --- Handshake authentication (ADR-0009, TASK-40) ---


def refused(harness: Harness, path: str, **kwargs: Any) -> int:
    """Status of the refused handshake; fails (never hangs) if the socket was accepted."""
    try:
        ws = harness.client.websocket_connect(path, **kwargs).__enter__()
    except WebSocketDenialResponse as denied:
        return int(denied.status_code)
    harness.open.append(ws)  # closed by the fixture
    raise AssertionError("handshake was accepted without a valid token")


def test_header_token_opens_the_stream_with_auth_ok(harness: Harness) -> None:
    session = harness.session()
    with harness.connect(session) as ws:
        ok = receive(ws)
        assert ws.accepted_subprotocol is None
    assert ok["type"] == "auth_ok" and ok["session_id"] == session["session_id"]
    assert ok["server_time_utc"].endswith("Z")


def test_browser_subprotocol_token_echoes_only_voa_v1(harness: Harness) -> None:
    session = harness.session()
    protocols = ["voa.v1", f"voa.bearer.{session['token']}"]
    path = f"/v1/streams/{session['session_id']}"
    with harness.client.websocket_connect(path, subprotocols=protocols) as ws:
        assert receive(ws)["type"] == "auth_ok"
        assert ws.accepted_subprotocol == "voa.v1"


@pytest.mark.parametrize(
    "case",
    ["no_token", "wrong_token", "other_session", "unknown_session", "basic_scheme",
     "bearer_without_voa_v1", "two_bearer_protocols"],
)  # fmt: skip
def test_bad_handshake_is_refused_with_401(harness: Harness, case: str) -> None:
    a, b = harness.session(), harness.session()
    path = f"/v1/streams/{a['session_id']}"
    options: dict[str, dict[str, Any]] = {
        "no_token": {},
        "wrong_token": {"headers": {"Authorization": "Bearer nope"}},
        "other_session": {"headers": {"Authorization": f"Bearer {b['token']}"}},
        "unknown_session": {"headers": {"Authorization": f"Bearer {a['token']}"}},
        "basic_scheme": {"headers": {"Authorization": f"Basic {a['token']}"}},
        "bearer_without_voa_v1": {"subprotocols": [f"voa.bearer.{a['token']}"]},
        "two_bearer_protocols": {
            "subprotocols": ["voa.v1", f"voa.bearer.{a['token']}", "voa.bearer.x"]
        },
    }
    kwargs = options[case]
    if case == "unknown_session":
        path = "/v1/streams/sess_unknown"
    assert refused(harness, path, **kwargs) == 401


def test_expired_token_is_refused_with_401(harness: Harness) -> None:
    session = harness.session()
    harness.mono.now += 900 * 1000 * MS  # VOA_TOKEN_TTL_S default 900
    path = f"/v1/streams/{session['session_id']}"
    headers = {"Authorization": f"Bearer {session['token']}"}
    assert refused(harness, path, headers=headers) == 401


def test_retired_auth_message_is_unsupported(harness: Harness) -> None:
    session = harness.session()
    ws = authed(harness, session)
    ws.send_json({"type": "auth", "token": session["token"]})
    assert receive(ws)["code"] == "unsupported_message_type"


def test_second_alert_sync_is_a_protocol_error_and_the_stream_stays_open(
    harness: Harness,
) -> None:
    ws = authed(harness, harness.session())
    ws.send_json({"type": "alert_sync", "last_alert_seq": 0})
    ws.send_json({"type": "alert_sync", "last_alert_seq": 0})
    assert receive(ws)["code"] == "protocol_error"
    ws.send_json({"type": "nope"})
    assert receive(ws)["code"] == "unsupported_message_type"  # still open


# --- Observations -> decisions and alerts (COM-02) ---


def test_three_obstacle_frames_push_decisions_then_one_alert(harness: Harness) -> None:
    session = harness.session()
    ws = authed(harness, session)
    pushed: list[dict[str, Any]] = []
    for _ in range(3):
        harness.mono.now += 100 * MS
        ws.send_json(harness.obstacle())
    while len([m for m in pushed if m["type"] in ("decision", "alert")]) < 3:
        pushed.append(receive(ws))
    ws.close()
    kinds = [(m["type"], m.get("decision", {}).get("state")) for m in pushed]
    assert kinds == [("decision", "candidate"), ("decision", "warning"), ("alert", None)]
    alert = pushed[2]
    assert alert["alert_seq"] == 1
    assert alert["scenario_id"] == OBSTACLE
    assert alert["alert"]["text"] == "전방 통로에 장애물이 보입니다."
    assert alert["alert"]["delivery_state"] == "queued"
    assert alert["risk_event_id"] == pushed[1]["decision"]["risk_event_id"]
    assert pushed[1]["decision"]["session_id"] == session["session_id"]
    assert pushed[1]["decision"]["device_id"] == "dev_example"


def test_input_loss_pushes_unknown_from_the_tick_loop(harness: Harness) -> None:
    ws = authed(harness, harness.session())
    for _ in range(3):
        harness.mono.now += 100 * MS
        ws.send_json(harness.obstacle())
    while receive(ws)["type"] != "alert":
        pass
    harness.mono.now += 1_501 * MS  # unknown_after_ms = 1500, no new frames
    decision = receive(ws)
    ws.close()
    assert decision["type"] == "decision"
    assert (decision["decision"]["state"], decision["decision"]["reason_codes"]) == (
        "unknown",
        ["input_stale"],
    )
    assert decision["decision"]["evidence_ids"] == []


def test_rejected_observation_returns_admission(harness: Harness) -> None:
    ws = authed(harness, harness.session())
    harness.mono.now += 100 * MS
    first = harness.obstacle()
    ws.send_json(first)
    receive(ws)  # candidate
    ws.send_json(first)  # same seq again
    admission = receive(ws)
    ws.close()
    assert admission == {
        "type": "admission",
        "seq": first["observation"]["seq"],
        "stream_id": "camera_0",
        "status": "out_of_order",
    }


def test_heartbeat_carries_states(harness: Harness) -> None:
    ws = authed(harness, harness.session())
    beat = receive(ws, skip=())
    ws.close()
    assert beat["type"] == "heartbeat"
    assert beat["states"] == {"moving_object_in_attention_zone": "unknown", OBSTACLE: "unknown"}


# --- Undeclared inputs (DEV-01, T-R17) ---


def test_undeclared_camera_keeps_vision_scenarios_unknown(harness: Harness) -> None:
    ws = authed(harness, harness.session(inputs=[]))
    for _ in range(5):
        harness.mono.now += 100 * MS
        ws.send_json(harness.obstacle())
        error = receive(ws)
        assert error == {
            "type": "error",
            "code": "stream_not_declared",
            "message": "stream was not declared for this session",
        }
    health = {
        "type": "observation",
        "observation": {
            "stream_id": "camera_0",
            "seq": 999,
            "capture_ts_monotonic_ns": harness.mono.now,
            "payload": {"kind": "health", "status": "ok", "detail_code": "x"},
        },
    }
    ws.send_json(health)  # cannot lift the injected "not_declared" down state
    assert receive(ws)["code"] == "stream_not_declared"
    beat = receive(ws, skip=())
    while beat["type"] != "heartbeat":
        beat = receive(ws, skip=())
    ws.close()
    assert set(beat["states"].values()) == {"unknown"}


def test_declared_session_injects_nothing(harness: Harness) -> None:
    session = harness.session(inputs=["camera_0"])
    runtime = harness.client.app.state.runtime  # type: ignore[attr-defined]
    assert runtime.streams[session["session_id"]].injected_ids == ()


def test_undeclared_streams_get_health_down_once(harness: Harness) -> None:
    session = harness.session(inputs=[])
    runtime = harness.client.app.state.runtime  # type: ignore[attr-defined]
    stream = runtime.streams[session["session_id"]]
    assert len(stream.injected_ids) == 1  # camera_0 is the only required stream
    observation = stream.engine.get_observation(stream.injected_ids[0])
    assert observation is not None
    assert observation.stream_id == "camera_0"
    assert (observation.payload.kind, observation.payload.status) == ("health", "down")
    assert observation.payload.detail_code == "not_declared"


# --- alert_sync and message type handling ---


def test_alert_sync_after_auth_is_accepted(harness: Harness) -> None:
    session = harness.session()
    ws = authed(harness, session)
    ws.send_json({"type": "alert_sync", "last_alert_seq": 0})
    ws.send_json({"type": "alert_sync", "last_alert_seq": -1})
    assert receive(ws)["code"] == "rejected_schema"  # only the invalid one answers
    ws.close()
    runtime = harness.client.app.state.runtime  # type: ignore[attr-defined]
    assert runtime.streams[session["session_id"]].last_alert_seq == 0


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"type": "frame", "frame_id": "f", "jpeg": "AAAA"}, "unsupported_message_type"),
        ({"type": "audio_chunk"}, "unsupported_message_type"),
        ({"type": "teleport"}, "unsupported_message_type"),
        ({"no_type": 1}, "unsupported_message_type"),
        ({"type": "observation", "observation": {"stream_id": "camera_0"}}, "rejected_schema"),
        ({"type": "ack", "alert_id": "a", "status": "lost"}, "rejected_schema"),
    ],
)
def test_bad_messages_get_an_error_and_the_stream_stays_open(
    harness: Harness, raw: dict[str, Any], code: str
) -> None:
    ws = authed(harness, harness.session())
    ws.send_json(raw)
    error = receive(ws)
    assert error["code"] == code
    assert json.dumps(raw) not in json.dumps(error)
    harness.mono.now += 100 * MS
    ws.send_json(harness.obstacle())
    assert receive(ws)["type"] == "decision"  # still serving
    ws.close()


def test_binary_and_non_json_frames_are_unsupported(harness: Harness) -> None:
    ws = authed(harness, harness.session())
    ws.send_bytes(b"\xff\xd8jpeg")
    assert receive(ws)["code"] == "unsupported_message_type"
    ws.send_text("not json")
    assert receive(ws)["code"] == "unsupported_message_type"
    ws.close()


# --- Metadata persistence ---


def test_observations_and_decisions_are_recorded(harness: Harness, workdir: Path) -> None:
    session = harness.session()
    ws = authed(harness, session)
    for _ in range(3):
        harness.mono.now += 100 * MS
        ws.send_json(harness.obstacle())
    while receive(ws)["type"] != "alert":
        pass
    ws.close()
    # A second, read-only connection; the server keeps its own on the event-loop thread.
    connection = sqlite3.connect(workdir / "var" / "voa.sqlite3")
    try:
        count = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("decisions", "observations_meta")
        }
    finally:
        connection.close()
    assert count == {"decisions": 2, "observations_meta": 3}
