"""TASK-21 input queue backpressure and the session status API (SPEC 7.5; COM-05, COM-06)."""

import asyncio
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voa.config.profile_loader import load_profile
from voa.contracts.observation import (
    Detection,
    DetectionPayload,
    HealthPayload,
    ModelRef,
    ObservationDraft,
)
from voa.engine.ports import FixedEpochClock, SequentialIdFactory
from voa.engine.profile import PolicyProfile
from voa.server import app as app_module
from voa.server.app import create_app
from voa.server.backpressure import InputQueue, QueuedObservation
from voa.server.session import SessionStream
from voa.server.settings import Settings

MS = 1_000_000
SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
OBSTACLE = "visible_obstacle_in_corridor"


def draft(stream: str = "camera_0", seq: int = 1, present: bool = True) -> ObservationDraft:
    return ObservationDraft(
        stream_id=stream,
        seq=seq,
        capture_ts_monotonic_ns=seq,
        model=ModelRef(id="mock-detector", revision="0"),
        payload=DetectionPayload(
            kind="object_detection",
            frame_id=f"{stream}-f{seq}",
            detections=[Detection(label="obstacle", bbox=(0.3, 0.5, 0.6, 0.9), score=0.9)]
            if present
            else [],
        ),
    )


def drain(queue: InputQueue) -> list[QueuedObservation]:
    items = []
    while (item := queue.pop()) is not None:
        items.append(item)
    return items


# --- Queue policy (unit) ---


def test_queue_keeps_items_in_order_until_full() -> None:
    q = InputQueue(maxsize=3)
    for seq in (1, 2, 3):
        assert q.offer(draft(seq=seq), received_mono_ns=seq)
    assert [i.draft.seq for i in drain(q)] == [1, 2, 3]
    assert q.dropped == {}


def test_full_queue_drops_the_oldest_video_and_counts_it() -> None:
    q = InputQueue(maxsize=2)
    q.offer(draft(seq=1), received_mono_ns=1)
    q.offer(draft(seq=2), received_mono_ns=2)
    assert q.offer(draft(seq=3), received_mono_ns=3)  # accepted, seq 1 dropped
    items = drain(q)
    assert [i.draft.seq for i in items] == [2, 3]
    assert q.dropped == {"camera_0": 1}


def test_only_the_next_accepted_video_of_that_stream_is_flagged() -> None:
    q = InputQueue(maxsize=2)
    q.offer(draft("camera_0", 1), received_mono_ns=1)
    q.offer(draft("camera_1", 1), received_mono_ns=2)
    q.offer(draft("camera_1", 2), received_mono_ns=3)  # drops camera_0 #1
    q.offer(draft("camera_0", 2), received_mono_ns=4)  # drops camera_1 #1
    q.offer(draft("camera_0", 3), received_mono_ns=5)  # drops camera_1 #2
    flags = {(i.draft.stream_id, i.draft.seq): i.draft.quality_flags for i in drain(q)}
    assert flags == {
        ("camera_0", 2): frozenset({"frame_dropped"}),  # first camera_0 after its drop
        ("camera_0", 3): frozenset(),  # pending was cleared
    }
    assert q.dropped == {"camera_0": 1, "camera_1": 2}
    assert q.drop_pending == {"camera_1"}  # no camera_1 frame accepted since


def test_incoming_frame_of_the_dropped_stream_carries_the_flag() -> None:
    q = InputQueue(maxsize=1)
    q.offer(draft(seq=1), received_mono_ns=1)
    q.offer(draft(seq=2), received_mono_ns=2)  # drops seq 1 then accepts seq 2 flagged
    (item,) = drain(q)
    assert (item.draft.seq, item.draft.quality_flags) == (2, frozenset({"frame_dropped"}))


def test_without_a_droppable_item_the_new_video_is_rejected() -> None:
    q = InputQueue(maxsize=1)
    q.offer(draft(seq=1), received_mono_ns=1, droppable=False)  # e.g. a frame being decoded
    assert not q.offer(draft(seq=2), received_mono_ns=2)
    assert [i.draft.seq for i in drain(q)] == [1]
    assert q.dropped == {"camera_0": 1} and q.drop_pending == {"camera_0"}
    assert q.offer(draft(seq=3), received_mono_ns=3)
    (item,) = drain(q)
    assert item.draft.quality_flags == frozenset({"frame_dropped"})


def test_flag_keeps_existing_quality_flags() -> None:
    q = InputQueue(maxsize=1)
    q.offer(draft(seq=1), received_mono_ns=1)
    flagged = draft(seq=2).model_copy(update={"quality_flags": frozenset({"low_light"})})
    q.offer(flagged, received_mono_ns=2)
    (item,) = drain(q)
    assert item.draft.quality_flags == frozenset({"low_light", "frame_dropped"})


def test_get_waits_for_an_item() -> None:
    async def scenario() -> int:
        q = InputQueue(maxsize=2)
        waiter = asyncio.create_task(q.get())
        await asyncio.sleep(0)
        assert not waiter.done()
        q.offer(draft(seq=7), received_mono_ns=1)
        return (await asyncio.wait_for(waiter, 1)).draft.seq

    assert asyncio.run(scenario()) == 7


# --- Through the session stream (COM-05: overload -> indeterminate) ---


def make_stream(queue_max: int = 1) -> SessionStream:
    profile = load_profile(CONFIG / "profiles" / "indoor_research_v0.yaml")
    assert isinstance(profile, PolicyProfile)
    return SessionStream.start(
        session_id="sess_1",
        device_id="dev_1",
        inputs=frozenset({"camera_0"}),
        profile=profile,
        ids=SequentialIdFactory(),
        clock=FixedEpochClock(),
        store=None,
        now_mono_ns=0,
        input_queue_max=queue_max,
    )


def test_dropped_frame_flag_makes_the_next_frame_indeterminate() -> None:
    stream = make_stream(queue_max=1)
    stream.input.offer(draft(seq=1), received_mono_ns=100 * MS)
    stream.input.offer(draft(seq=2), received_mono_ns=200 * MS)  # overload drops seq 1
    item = stream.input.pop()
    assert item is not None
    messages = stream.ingest(item.draft, 200 * MS, received_mono_ns=item.received_mono_ns)
    assert messages == []  # frame_dropped blocks: no candidate from an obstacle frame
    assert stream.engine.states()[OBSTACLE] == "unknown"


def test_queue_delay_counts_against_freshness() -> None:
    stream = make_stream(queue_max=4)
    stream.input.offer(draft(seq=1), received_mono_ns=0)
    item = stream.input.pop()
    assert item is not None
    messages = stream.ingest(item.draft, 1_001 * MS, received_mono_ns=item.received_mono_ns)
    assert [m.type for m in messages] == ["admission"]
    assert messages[0].status == "stale_on_arrival"  # type: ignore[union-attr]


# --- Health bypasses the saturated queue; status API ---


class FakeMono:
    def __init__(self) -> None:
        self.now = 10_000 * MS

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Server whose input loop never consumes, so the video queue stays saturated."""
    shutil.copytree(CONFIG / "profiles", tmp_path / "config" / "profiles")
    shutil.copytree(CONFIG / "models", tmp_path / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", tmp_path / "config" / "devices.yaml")

    async def stalled(*_: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "input_loop", stalled)
    mono = FakeMono()
    settings = Settings.from_env(
        {"VOA_INPUT_QUEUE_MAX": "2", "VOA_TICK_INTERVAL_MS": "10"}, base_dir=tmp_path
    )
    with TestClient(create_app(lambda: settings, mono_ns=mono)) as client:
        yield client, mono, tmp_path


def _session(client: TestClient) -> dict[str, Any]:
    return dict(
        client.post(
            "/v1/sessions",
            json={
                "device_id": "dev_example",
                "secret": "example-secret-change-me",
                "capabilities": {"inputs": ["camera_0"], "outputs": ["hud"]},
            },
        ).json()
    )


def _observation(d: ObservationDraft) -> dict[str, Any]:
    return {"type": "observation", "observation": d.model_dump(mode="json")}


def test_health_bypasses_a_saturated_video_queue(server: Any) -> None:
    client, mono, workdir = server
    session = _session(client)
    with client.websocket_connect(f"/v1/streams/{session['session_id']}") as ws:
        ws.send_json({"type": "auth", "token": session["token"]})
        assert ws.receive_json()["type"] == "auth_ok"
        for seq in range(1, 6):  # queue max 2: three drops, nothing consumed
            ws.send_json(_observation(draft(seq=seq)))
        health = ObservationDraft(
            stream_id="camera_0",
            seq=1,
            capture_ts_monotonic_ns=1,
            payload=HealthPayload(kind="health", status="down", detail_code="x"),
        )
        ws.send_json(_observation(health))
        ws.send_json({"type": "alert_sync", "last_alert_seq": 0})  # control path round trip
        mono.now += 1 * MS
        status = client.get(
            f"/v1/sessions/{session['session_id']}/status",
            headers={"Authorization": f"Bearer {session['token']}"},
        ).json()
    runtime = client.app.state.runtime
    assert runtime.streams[session["session_id"]].input.dropped == {"camera_0": 3}
    connection = sqlite3.connect(workdir / "var" / "voa.sqlite3")
    try:
        rows = connection.execute(
            "SELECT source_kind, admission_status FROM observations_meta"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [("health", "accepted")]  # processed although the video queue is full
    assert status["streams"] == [
        {"stream_id": "camera_0", "last_received_age_ms": 1, "dropped_frames": 3}
    ]


def test_status_response_fields(server: Any) -> None:
    client, _, _ = server
    session = _session(client)
    sid = session["session_id"]
    runtime = client.app.state.runtime
    stream = runtime.streams[sid]
    # Make one pending and one played alert directly through the stream (no sockets).
    for seq in range(1, 4):
        client.portal.call(stream.ingest, draft(seq=seq), runtime.mono_ns())
    (record,) = stream.dispatcher.records.values()
    response = client.get(
        f"/v1/sessions/{sid}/status", headers={"Authorization": f"Bearer {session['token']}"}
    )
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"session_id", "device_id", "scenarios", "streams", "pending_alerts"}
    assert (body["session_id"], body["device_id"]) == (sid, "dev_example")
    assert body["scenarios"] == [
        {"scenario_id": "moving_object_in_attention_zone", "state": "unknown"},
        {"scenario_id": OBSTACLE, "state": "warning"},
    ]
    # ingest() was called directly, so no socket receipt was recorded.
    assert body["streams"] == [
        {"stream_id": "camera_0", "last_received_age_ms": None, "dropped_frames": 0}
    ]
    assert body["pending_alerts"] == [
        {
            "alert_id": record.alert_id,
            "alert_seq": 1,
            "delivery_state": "queued",
            "valid_until_utc": record.alert.valid_until_utc.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert "text" not in response.text and session["token"] not in response.text
    client.portal.call(stream.ack, record.alert_id, "played", runtime.mono_ns())
    played = client.get(
        f"/v1/sessions/{sid}/status", headers={"Authorization": f"Bearer {session['token']}"}
    ).json()
    assert played["pending_alerts"] == []


def test_status_without_receipts_reports_null_age(server: Any) -> None:
    client, _, _ = server
    session = _session(client)
    body = client.get(
        f"/v1/sessions/{session['session_id']}/status",
        headers={"Authorization": f"Bearer {session['token']}"},
    ).json()
    assert body["streams"] == [
        {"stream_id": "camera_0", "last_received_age_ms": None, "dropped_frames": 0}
    ]


def test_status_requires_the_sessions_own_token(server: Any) -> None:
    client, _, _ = server
    a, b = _session(client), _session(client)
    url = f"/v1/sessions/{a['session_id']}/status"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401
    other = client.get(url, headers={"Authorization": f"Bearer {b['token']}"})
    assert (other.status_code, other.json()) == (404, {"code": "not_found"})
