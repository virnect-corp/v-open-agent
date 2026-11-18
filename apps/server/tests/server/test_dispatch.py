"""TASK-19 alert delivery, ACK and degraded notices (SPEC 7.3, 7.4; T-R11, T-R18)."""

import logging
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from voa.contracts.decision import Alert, RiskDecision
from voa.contracts.enums import DeliveryState, RiskState
from voa.server.app import create_app
from voa.server.dispatch import AlertDispatcher, NoticeTracker
from voa.server.settings import Settings

MS = 1_000_000
SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
OBSTACLE = "visible_obstacle_in_corridor"
NOTICE_TEXT = "카메라 상태를 확인할 수 없어 감지가 제한됩니다."


def decision(
    state: RiskState, previous: RiskState | None, *, alert_id: str | None = None
) -> RiskDecision:
    return RiskDecision(
        schema_version="0.1.0",
        risk_event_id="risk_1",
        session_id="s",
        device_id="d",
        scenario_id=OBSTACLE,
        policy_version="p@1",
        state=state,
        previous_state=previous,
        severity="attention" if state == "warning" else None,
        evidence_ids=["o1"] if state == "warning" else [],
        reason_codes=["input_stale"],
        decided_at_utc=datetime(2026, 11, 9, tzinfo=UTC),
        alert=Alert(
            alert_id=alert_id or "alert_1",
            text="t",
            text_kind="approved_template",
            delivery_state="queued",
        )
        if state == "warning"
        else None,
    )


def dispatcher_with(state: DeliveryState) -> AlertDispatcher:
    """Reach `state` for alert_1 through public transitions (ttl 1000 ms from t=0)."""
    d = AlertDispatcher()
    d.queue(decision("warning", "candidate"), 1, now_mono_ns=0, ttl_ms=1000)
    if state in ("sent", "received", "played", "unknown_delivery"):
        d.mark_sent("alert_1")
    if state in ("received", "played"):
        d.ack("alert_1", "received")
    if state == "played":
        d.ack("alert_1", "played")
    if state in ("expired", "unknown_delivery"):
        d.expire(1000 * MS)
    assert d.records["alert_1"].state == state
    return d


# --- Delivery state machine (unit) ---


def test_queue_then_sent() -> None:
    d = dispatcher_with("queued")
    assert d.mark_sent("alert_1") and d.records["alert_1"].state == "sent"
    assert not d.mark_sent("alert_1")  # only from queued
    assert not d.mark_sent("alert_unknown")


@pytest.mark.parametrize(
    ("start", "after_expiry"),
    [
        ("queued", "expired"),
        ("sent", "unknown_delivery"),
        ("received", "received"),
        ("played", "played"),
    ],
)
def test_expiry(start: DeliveryState, after_expiry: DeliveryState) -> None:
    d = dispatcher_with(start)
    assert d.expire(1000 * MS - 1) == []  # before valid_until nothing changes
    d.expire(1000 * MS)
    assert d.records["alert_1"].state == after_expiry


def test_tr11_received_without_played_is_never_reported_as_played() -> None:
    d = dispatcher_with("received")
    d.expire(10_000 * MS)
    assert d.records["alert_1"].state == "received"


@pytest.mark.parametrize(
    ("start", "status", "end", "changed"),
    [
        ("sent", "received", "received", True),
        ("sent", "played", "played", True),
        ("received", "played", "played", True),
        ("received", "received", "received", False),  # duplicate
        ("played", "played", "played", False),  # duplicate
        ("played", "received", "played", False),  # no regression
    ],
)
def test_ack_moves_forward_only(
    start: DeliveryState, status: str, end: DeliveryState, changed: bool
) -> None:
    d = dispatcher_with(start)
    outcome = d.ack("alert_1", status)  # type: ignore[arg-type]
    assert (d.records["alert_1"].state, outcome.changed, outcome.late) == (end, changed, False)


@pytest.mark.parametrize(
    ("start", "status"),
    [
        ("unknown_delivery", "received"),
        ("unknown_delivery", "played"),
        ("expired", "received"),
        ("expired", "played"),
    ],
)
def test_late_ack_restores_state_and_logs(
    start: DeliveryState, status: str, caplog: pytest.LogCaptureFixture
) -> None:
    d = dispatcher_with(start)
    with caplog.at_level(logging.INFO, logger="voa.server.dispatch"):
        outcome = d.ack("alert_1", status)  # type: ignore[arg-type]
    assert (d.records["alert_1"].state, outcome.late) == (status, True)
    record = caplog.records[-1]
    assert (record.event, record.alert_id, record.status) == ("late_ack", "alert_1", status)  # type: ignore[attr-defined]


def test_ack_for_unknown_alert_is_ignored() -> None:
    assert not AlertDispatcher().ack("alert_x", "played").changed


# --- Notice conditions (unit, T-R18) ---


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ([("candidate", "unknown")], [False, True]),
        ([("warning", "unknown")], [False, True]),
        ([("clear", "unknown")], [False, False]),
        ([("resolved", "unknown")], [False, False]),
    ],
)
def test_notice_only_from_candidate_or_warning(
    sequence: list[tuple[RiskState, RiskState]], expected: list[bool]
) -> None:
    tracker = NoticeTracker()
    previous, state = sequence[0]
    got = [
        tracker.should_notify(decision(previous, "unknown")),
        tracker.should_notify(decision(state, previous)),
    ]
    assert got == expected


def test_notice_once_until_unknown_is_left() -> None:
    tracker = NoticeTracker()
    assert tracker.should_notify(decision("unknown", "warning"))
    assert not tracker.should_notify(decision("unknown", "warning"))  # not twice
    tracker.should_notify(decision("candidate", "unknown"))  # leaves unknown
    assert tracker.should_notify(decision("unknown", "candidate"))


# --- Integration over WebSocket and REST ---


class FakeMono:
    def __init__(self) -> None:
        self.now = 10_000 * MS

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[TestClient, FakeMono, Path]]:
    shutil.copytree(CONFIG / "profiles", tmp_path / "config" / "profiles")
    shutil.copytree(CONFIG / "models", tmp_path / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", tmp_path / "config" / "devices.yaml")
    mono = FakeMono()
    settings = Settings.from_env(
        {"VOA_TICK_INTERVAL_MS": "10", "VOA_HEARTBEAT_MS": "1000"}, base_dir=tmp_path
    )
    with TestClient(create_app(lambda: settings, mono_ns=mono)) as client:
        yield client, mono, tmp_path


@contextmanager
def connected(client: TestClient) -> Iterator[tuple[dict[str, Any], WebSocketTestSession]]:
    session = client.post(
        "/v1/sessions",
        json={
            "device_id": "dev_example",
            "secret": "example-secret-change-me",
            "capabilities": {"inputs": ["camera_0"], "outputs": ["hud"]},
        },
    ).json()
    with client.websocket_connect(f"/v1/streams/{session['session_id']}") as ws:
        ws.send_json({"type": "auth", "token": session["token"]})
        assert next_of(ws, "auth_ok")
        yield session, ws


def next_of(ws: WebSocketTestSession, *types: str) -> dict[str, Any]:
    while True:
        message = dict(ws.receive_json())
        if message["type"] in types:
            return message


class Frames:
    def __init__(self, ws: WebSocketTestSession, mono: FakeMono) -> None:
        self.ws, self.mono, self.seq = ws, mono, 0

    def send(self, present: bool, step_ms: int = 100) -> None:
        self.mono.now += step_ms * MS
        self.seq += 1
        detections = (
            [{"label": "obstacle", "bbox": [0.3, 0.5, 0.6, 0.9], "score": 0.9}] if present else []
        )
        self.ws.send_json(
            {
                "type": "observation",
                "observation": {
                    "stream_id": "camera_0",
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
        )


def alert_state(workdir: Path, alert_id: str) -> str | None:
    connection = sqlite3.connect(workdir / "var" / "voa.sqlite3")
    try:
        row = connection.execute(
            "SELECT delivery_state FROM alerts WHERE alert_id = ?", (alert_id,)
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def warn(frames: Frames) -> dict[str, Any]:
    for _ in range(3):
        frames.send(True)
    return next_of(frames.ws, "alert")


def runtime_state(client: TestClient, session: dict[str, Any], alert_id: str) -> str:
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    return str(runtime.streams[session["session_id"]].dispatcher.records[alert_id].state)


def test_sent_alert_is_recorded_and_acked_over_websocket(
    server: tuple[TestClient, FakeMono, Path],
) -> None:
    client, mono, workdir = server
    with connected(client) as (session, ws):
        alert = warn(Frames(ws, mono))
        alert_id = alert["alert"]["alert_id"]
        ws.send_json({"type": "ack", "alert_id": alert_id, "status": "received"})
        ws.send_json({"type": "ack", "alert_id": "alert_unknown", "status": "played"})  # ignored
        ws.send_json({"type": "ack", "alert_id": alert_id, "status": "played"})
        Frames(ws, mono).send(True, step_ms=0)  # round trip so the ACKs are processed
        next_of(ws, "admission", "decision", "error")
        assert runtime_state(client, session, alert_id) == "played"
    assert alert_state(workdir, alert_id) == "played"


def test_alert_expires_to_unknown_delivery_then_late_ack_recovers(
    server: tuple[TestClient, FakeMono, Path], caplog: pytest.LogCaptureFixture
) -> None:
    client, mono, workdir = server
    with connected(client) as (session, ws):
        alert_id = warn(Frames(ws, mono))["alert"]["alert_id"]
        assert runtime_state(client, session, alert_id) == "sent"
        mono.now += 4_000 * MS  # warning_ttl_ms; also past unknown_after_ms
        next_of(ws, "notice")  # tick ran: warning -> unknown -> notice
        assert runtime_state(client, session, alert_id) == "unknown_delivery"
        with caplog.at_level(logging.INFO, logger="voa.server.dispatch"):
            response = client.post(
                f"/v1/alerts/{alert_id}/ack",
                json={"status": "received"},
                headers={"Authorization": f"Bearer {session['token']}"},
            )
        assert response.json() == {"alert_id": alert_id, "delivery_state": "received"}
    assert any(getattr(r, "event", None) == "late_ack" for r in caplog.records)
    assert alert_state(workdir, alert_id) == "received"


def test_rest_ack_requires_the_owning_sessions_token(
    server: tuple[TestClient, FakeMono, Path],
) -> None:
    client, mono, _ = server
    with connected(client) as (session, ws):
        alert_id = warn(Frames(ws, mono))["alert"]["alert_id"]
        url = f"/v1/alerts/{alert_id}/ack"
        body = {"status": "played"}
        no_auth = client.post(url, json=body)
        wrong = client.post(url, json=body, headers={"Authorization": "Bearer nope"})
        with connected(client) as (other, _):
            foreign = client.post(
                url, json=body, headers={"Authorization": f"Bearer {other['token']}"}
            )
        missing = client.post(
            "/v1/alerts/alert_x/ack",
            json=body,
            headers={"Authorization": f"Bearer {session['token']}"},
        )
        bad_status = client.post(
            url, json={"status": "lost"}, headers={"Authorization": f"Bearer {session['token']}"}
        )
        ok = client.post(url, json=body, headers={"Authorization": f"Bearer {session['token']}"})
    assert (no_auth.status_code, no_auth.json()) == (401, {"code": "unauthorized"})
    assert wrong.status_code == 401
    assert (foreign.status_code, foreign.json()) == (404, {"code": "not_found"})
    assert (missing.status_code, missing.json()) == (404, {"code": "not_found"})
    assert (bad_status.status_code, bad_status.json()) == (400, {"code": "invalid_request"})
    assert ok.json() == {"alert_id": alert_id, "delivery_state": "played"}


def test_tr18_notice_once_for_warning_loss_and_never_for_clear_loss(
    server: tuple[TestClient, FakeMono, Path],
) -> None:
    client, mono, _ = server
    with connected(client) as (_, ws):
        frames = Frames(ws, mono)
        warn(frames)
        mono.now += 1_600 * MS  # input lost while warning
        notice = next_of(ws, "notice")
        assert notice["kind"] == "degraded" and notice["scenario_id"] == OBSTACLE
        assert notice["text"] == NOTICE_TEXT

        for _ in range(3):  # fresh absent frames: unknown -> clear
            frames.send(False)
        clear = next_of(ws, "decision")
        assert clear["decision"]["state"] == "clear"
        mono.now += 1_600 * MS  # input lost while clear
        unknown = next_of(ws, "decision", "notice")
        assert unknown["type"] == "decision" and unknown["decision"]["state"] == "unknown"
        frames.send(True, step_ms=0)  # flush: nothing but candidate decision follows
        follow = next_of(ws, "decision", "notice")
        assert follow["type"] == "decision" and follow["decision"]["state"] == "candidate"
