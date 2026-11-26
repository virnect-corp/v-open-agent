"""TASK-20 reconnect and duplicate prevention: alert_sync (SPEC 7.3, ADR-0006; COM-04, T-R10)."""

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from voa.config.profile_loader import load_profile
from voa.contracts.observation import Detection, DetectionPayload, ModelRef, ObservationDraft
from voa.engine.ports import FixedEpochClock, SequentialIdFactory
from voa.engine.profile import PolicyProfile
from voa.server.app import create_app
from voa.server.session import SessionStream
from voa.server.settings import Settings

MS = 1_000_000
SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
OBSTACLE = "visible_obstacle_in_corridor"


class Stream:
    """A SessionStream driven directly, without sockets (all conditions are synchronous)."""

    def __init__(self) -> None:
        profile = load_profile(CONFIG / "profiles" / "indoor_research_v0.yaml")
        assert isinstance(profile, PolicyProfile)
        self.now = 0
        self.seq = 0
        self.stream = SessionStream.start(
            session_id="sess_1",
            device_id="dev_1",
            inputs=frozenset({"camera_0"}),
            profile=profile,
            ids=SequentialIdFactory(),
            clock=FixedEpochClock(),
            store=None,
            now_mono_ns=0,
        )

    def frame(self, present: bool, step_ms: int = 100) -> list[Any]:
        self.now += step_ms * MS
        self.seq += 1
        draft = ObservationDraft(
            stream_id="camera_0",
            seq=self.seq,
            capture_ts_monotonic_ns=self.now,
            model=ModelRef(id="mock-detector", revision="0"),
            payload=DetectionPayload(
                kind="object_detection",
                frame_id=f"f{self.seq}",
                detections=[Detection(label="obstacle", bbox=(0.3, 0.5, 0.6, 0.9), score=0.9)]
                if present
                else [],
            ),
        )
        return self.stream.ingest(draft, self.now)

    def warn(self) -> str:
        alerts = [m for _ in range(3) for m in self.frame(True) if m.type == "alert"]
        assert len(alerts) == 1
        return str(alerts[0].alert.alert_id)

    def sync(self, last_alert_seq: int) -> list[tuple[int, str]]:
        return [
            (m.alert_seq, m.alert.alert_id)
            for m in self.stream.alert_sync(last_alert_seq, self.now)
        ]


# --- Re-send rules (SPEC 7.3 conditions 1/2/3) ---


def test_valid_alert_is_resent_with_the_same_id_and_seq() -> None:
    s = Stream()
    alert_id = s.warn()
    assert s.sync(0) == [(1, alert_id)]
    assert s.stream.last_alert_seq == 0


def test_alerts_up_to_last_alert_seq_are_not_resent() -> None:
    s = Stream()
    s.warn()
    assert s.sync(1) == []
    assert s.sync(5) == []


def test_condition1_scenario_no_longer_warning() -> None:
    s = Stream()
    s.warn()
    s.stream.tick(s.now + 1_501 * MS)  # input lost: warning -> unknown, alert still valid
    s.now += 1_501 * MS
    assert s.stream.engine.states()[OBSTACLE] == "unknown"
    assert s.sync(0) == []


def test_condition2_only_the_latest_alert_of_the_episode() -> None:
    s = Stream()
    first = s.warn()
    s.now += 1_501 * MS
    s.stream.tick(s.now)  # unknown, same episode continues
    second = s.warn()  # unknown -> candidate -> warning: new alert, same risk_event_id
    assert first != second
    assert s.sync(0) == [(2, second)]


def test_condition3_expired_alert_is_not_resent_even_while_warning() -> None:
    s = Stream()
    s.warn()
    for _ in range(40):  # keep the obstacle in view past warning_ttl_ms (4000 ms)
        s.frame(True)
    assert s.stream.engine.states()[OBSTACLE] == "warning"
    assert s.sync(0) == []


def test_alert_created_while_disconnected_stays_queued_until_synced() -> None:
    s = Stream()
    alert_id = s.warn()  # nobody wrote it to a socket
    assert s.stream.dispatcher.records[alert_id].state == "queued"
    assert s.sync(0) == [(1, alert_id)]
    s.stream.mark_sent(alert_id, s.now)
    assert s.stream.dispatcher.records[alert_id].state == "sent"


def test_played_alert_is_still_resent_and_keeps_its_state() -> None:
    """The device dedups by alert_id and only re-ACKs; the server state does not regress."""
    s = Stream()
    alert_id = s.warn()
    s.stream.mark_sent(alert_id, s.now)
    s.stream.ack(alert_id, "played", s.now)
    assert s.sync(0) == [(1, alert_id)]
    s.stream.mark_sent(alert_id, s.now)
    assert s.stream.dispatcher.records[alert_id].state == "played"


# --- Reconnect over the real endpoints (UC-05) ---


class FakeMono:
    def __init__(self) -> None:
        self.now = 10_000 * MS

    def __call__(self) -> int:
        return self.now


def _observation(seq: int, now: int) -> dict[str, Any]:
    return {
        "type": "observation",
        "observation": {
            "stream_id": "camera_0",
            "seq": seq,
            "capture_ts_monotonic_ns": now,
            "model": {"id": "mock-detector", "revision": "0"},
            "payload": {
                "kind": "object_detection",
                "frame_id": f"f{seq}",
                "detections": [{"label": "obstacle", "bbox": [0.3, 0.5, 0.6, 0.9], "score": 0.9}],
            },
        },
    }


def _next(ws: Any, kind: str) -> dict[str, Any]:
    while (message := dict(ws.receive_json()))["type"] != kind:
        pass
    return message


def test_reconnect_refresh_auth_alert_sync_resends_the_same_alert(tmp_path: Path) -> None:
    shutil.copytree(CONFIG / "profiles", tmp_path / "config" / "profiles")
    shutil.copytree(CONFIG / "models", tmp_path / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", tmp_path / "config" / "devices.yaml")
    mono = FakeMono()
    settings = Settings.from_env({"VOA_TICK_INTERVAL_MS": "10"}, base_dir=tmp_path)
    with TestClient(create_app(lambda: settings, mono_ns=mono)) as client:
        session = client.post(
            "/v1/sessions",
            json={
                "device_id": "dev_example",
                "secret": "example-secret-change-me",
                "capabilities": {"inputs": ["camera_0"], "outputs": ["hud"]},
            },
        ).json()
        sid = session["session_id"]
        bearer = {"Authorization": f"Bearer {session['token']}"}
        with client.websocket_connect(f"/v1/streams/{sid}", headers=bearer) as ws:
            _next(ws, "auth_ok")
            for seq in range(1, 4):
                mono.now += 100 * MS
                ws.send_json(_observation(seq, mono.now))
            first = _next(ws, "alert")
        # Connection dropped. Rotate tokens, reconnect, and sync from before the alert.
        refreshed = client.post(
            f"/v1/sessions/{sid}/refresh", json={"refresh_token": session["refresh_token"]}
        ).json()
        reuse = client.post(
            f"/v1/sessions/{sid}/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert reuse.status_code == 401  # the previous refresh token is gone
        bearer = {"Authorization": f"Bearer {refreshed['token']}"}
        with client.websocket_connect(f"/v1/streams/{sid}", headers=bearer) as ws:
            _next(ws, "auth_ok")
            ws.send_json({"type": "alert_sync", "last_alert_seq": 0})
            again = _next(ws, "alert")
            ws.send_json({"type": "resume", "last_alert_seq": 0})  # old name
            assert _next(ws, "error")["code"] == "unsupported_message_type"
    assert again["alert"]["alert_id"] == first["alert"]["alert_id"]
    assert again["alert_seq"] == first["alert_seq"] == 1


def test_the_old_session_token_is_refused_after_refresh(tmp_path: Path) -> None:
    """Rotation retires the previous access token for the handshake too (ADR-0005, 0009)."""
    shutil.copytree(CONFIG / "profiles", tmp_path / "config" / "profiles")
    shutil.copytree(CONFIG / "models", tmp_path / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", tmp_path / "config" / "devices.yaml")
    settings = Settings.from_env({}, base_dir=tmp_path)
    with TestClient(create_app(lambda: settings)) as client:
        session = client.post(
            "/v1/sessions",
            json={
                "device_id": "dev_example",
                "secret": "example-secret-change-me",
                "capabilities": {"inputs": ["camera_0"], "outputs": ["hud"]},
            },
        ).json()
        sid = session["session_id"]
        client.post(f"/v1/sessions/{sid}/refresh", json={"refresh_token": session["refresh_token"]})
        old = {"Authorization": f"Bearer {session['token']}"}
        with pytest.raises(WebSocketDenialResponse) as denied:
            client.websocket_connect(f"/v1/streams/{sid}", headers=old).__enter__()
    assert denied.value.status_code == 401
