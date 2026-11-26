"""TASK-42 frame upload and server-side inference (ADR-0009, SPEC 7.6; INP-01, DAT-01).

The detector is a fake that reads marker bytes after a minimal JPEG header, so no
image library or weights are needed. The default mission is `indoor_research_v0`;
its `visible_obstacle_in_corridor` scenario uses only `mock-detector`.
"""

import logging
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from voa.config.model_registry import ModelManifest
from voa.providers.rtdetr import RawDetection, WeightsError
from voa.server.app import create_app
from voa.server.settings import Settings

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
SECRET = "example-secret-change-me"
CAPS = {"inputs": ["camera_0"], "outputs": ["hud"]}
OBSTACLE_BOX = (192.0, 180.0, 384.0, 324.0)  # inside the corridor ROI on 640x360
CANARY = b"CANARY-FRAME-BYTES-5d1e"


def jpeg(marker: bytes = b"", width: int = 640, height: int = 360) -> bytes:
    """SOI + SOF0 with the size, then marker bytes the fake detector reads."""
    sof = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
    return b"\xff\xd8" + sof + b"\x03" + b"\x00" * 9 + marker


class FakeDetector:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def detect(self, image: Any) -> tuple[int, int, list[RawDetection]]:
        data = bytes(image)
        if b"SLOW" in data:
            time.sleep(0.4)
        found = []
        if b"OBSTACLE" in data:
            found.append(RawDetection("obstacle", 0.9, OBSTACLE_BOX))
        return 640, 360, found


def factory(manifest: ModelManifest, weights_root: Path) -> FakeDetector:
    if b"broken" in manifest.model_id.encode():
        raise WeightsError("missing")
    return FakeDetector(manifest.model_id)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    shutil.copytree(CONFIG / "profiles", tmp_path / "config" / "profiles")
    shutil.copytree(CONFIG / "models", tmp_path / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", tmp_path / "config" / "devices.yaml")
    return tmp_path


def make_client(workdir: Path, **env: str) -> TestClient:
    base = {"VOA_HEARTBEAT_MS": "50", "VOA_CLIENT_WATCHDOG_MS": "100"}
    base.update({f"VOA_{k}": v for k, v in env.items()})
    settings = Settings.from_env(base, base_dir=workdir)
    return TestClient(create_app(lambda: settings, detector_factory=factory))


@pytest.fixture
def client(workdir: Path) -> Iterator[TestClient]:
    with make_client(workdir) as c:
        yield c


class Session:
    def __init__(self, client: TestClient, **extra: Any) -> None:
        body = {"device_id": "dev_example", "secret": SECRET, "capabilities": CAPS, **extra}
        created = client.post("/v1/sessions", json=body).json()
        self.client = client
        self.id = created["session_id"]
        self.token = created["token"]
        self.created = created
        self.seq = 0

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def connect(self) -> WebSocketTestSession:
        return self.client.websocket_connect(f"/v1/streams/{self.id}", headers=self.auth)

    def upload(self, data: bytes, **headers: str) -> Any:
        self.seq += 1
        base = {
            **self.auth,
            "Content-Type": "image/jpeg",
            "X-VOA-Stream-Id": "camera_0",
            "X-VOA-Seq": str(self.seq),
            "X-VOA-Frame-Id": f"f_{self.seq:06d}",
            "X-VOA-Capture-Ts-Ns": str(self.seq * 333_000_000),
        }
        base.update(headers)
        return self.client.post(f"/v1/sessions/{self.id}/frames", content=data, headers=base)


def next_of(ws: WebSocketTestSession, *types: str) -> dict[str, Any]:
    while True:
        message = dict(ws.receive_json())
        if message["type"] in types:
            return message


def test_session_announces_the_frame_interval(client: TestClient) -> None:
    assert Session(client).created["frame_interval_ms"] == 333


def test_three_obstacle_frames_raise_one_alert_with_evidence_and_timing(client: TestClient) -> None:
    s = Session(client)
    with s.connect() as ws:
        next_of(ws, "auth_ok")
        responses = [s.upload(jpeg(b"OBSTACLE")) for _ in range(3)]
        alert = next_of(ws, "alert")
    assert [r.status_code for r in responses] == [200, 200, 200]
    last = responses[-1].json()
    assert last["frame_id"] == "f_000003" and last["seq"] == 3 and last["status"] == "processed"
    [result] = last["results"]
    assert result["model_id"] == "mock-detector"
    assert result["detections"][0]["label"] == "obstacle"
    assert last["states"]["visible_obstacle_in_corridor"] == "warning"
    timing = last["timing"]
    assert timing["capture_ts_monotonic_ns"] == 3 * 333_000_000
    assert (
        timing["received_at_utc"]
        <= timing["processing_started_at_utc"]
        <= timing["completed_at_utc"]
    )
    assert timing["queue_ms"] >= 0 and timing["provider_ms"] >= 0 and timing["engine_us"] >= 0
    assert alert["scenario_id"] == "visible_obstacle_in_corridor"


def test_uploads_need_a_mission_a_control_socket_and_the_right_token(workdir: Path) -> None:
    with make_client(workdir, DEFAULT_MISSION="-") as client:
        s, other = Session(client), Session(client)
        assert (
            s.upload(jpeg()).status_code == 409
            and s.upload(jpeg()).json()["code"] == "mission_not_selected"
        )
        client.put(
            f"/v1/sessions/{s.id}/mission",
            json={"mission_id": "indoor_research_v0"},
            headers=s.auth,
        )
        assert s.upload(jpeg()).json() == {"code": "control_not_connected"}
        with s.connect() as ws:
            next_of(ws, "auth_ok")
            assert s.upload(jpeg()).status_code == 200
            wrong = s.upload(jpeg(), Authorization=f"Bearer {other.token}")
            assert wrong.status_code == 401


@pytest.mark.parametrize(
    ("data", "headers", "code"),
    [
        (b"not a jpeg", {}, "frame_rejected"),
        (jpeg(width=1280), {}, "frame_rejected"),
        (jpeg(), {"Content-Type": "image/png"}, "frame_rejected"),
        (jpeg(), {"X-VOA-Seq": "x"}, "frame_rejected"),
        (jpeg(), {"X-VOA-Quality-Flags": "made_up"}, "frame_rejected"),
        (jpeg(), {"X-VOA-Stream-Id": "mic_0"}, "stream_not_declared"),
    ],
)
def test_bad_uploads_get_stable_codes(
    client: TestClient, data: bytes, headers: dict[str, str], code: str
) -> None:
    s = Session(client)
    with s.connect() as ws:
        next_of(ws, "auth_ok")
        response = s.upload(data, **headers)
    assert response.status_code == 400 and response.json() == {"code": code}


def test_frames_larger_than_the_limit_or_out_of_order_are_refused(workdir: Path) -> None:
    with make_client(workdir, FRAME_MAX_BYTES="100") as client:
        s = Session(client)
        with s.connect() as ws:
            next_of(ws, "auth_ok")
            assert s.upload(jpeg(b"x" * 200)).json() == {"code": "frame_rejected"}
            assert s.upload(jpeg()).status_code == 200  # seq 2
            assert s.upload(jpeg(), **{"X-VOA-Seq": "1"}).json() == {"code": "out_of_order"}


def test_a_second_frame_while_one_is_in_flight_is_busy_not_queued(client: TestClient) -> None:
    s = Session(client)
    with s.connect() as ws:
        next_of(ws, "auth_ok")
        slow: dict[str, Any] = {}
        worker = threading.Thread(target=lambda: slow.update(r=s.upload(jpeg(b"SLOW"))))
        worker.start()
        time.sleep(0.15)
        busy = s.upload(jpeg())
        worker.join()
        for _ in range(50):  # heartbeats sent before the counters changed come first
            frames = next_of(ws, "heartbeat")["frames"]
            if frames["processed"] == 1:
                break
    assert busy.status_code == 409 and busy.json() == {"code": "frame_busy"}
    assert slow["r"].status_code == 200
    assert frames == {"received": 2, "processed": 1, "skipped": 1, "rejected": 0}


def test_slow_inference_answers_202_and_sends_frame_result(workdir: Path) -> None:
    with make_client(workdir, FRAME_SYNC_TIMEOUT_MS="50") as client:
        s = Session(client)
        with s.connect() as ws:
            next_of(ws, "auth_ok")
            accepted = s.upload(jpeg(b"SLOW OBSTACLE"))
            assert accepted.status_code == 202
            assert accepted.json() == {"frame_id": "f_000001", "status": "accepted"}
            late = next_of(ws, "frame_result")
    assert late["frame_id"] == "f_000001" and late["results"][0]["detections"]


def test_quality_flagged_frames_are_not_evidence(client: TestClient) -> None:
    s = Session(client)
    with s.connect() as ws:
        next_of(ws, "auth_ok")
        responses = [
            s.upload(jpeg(b"OBSTACLE"), **{"X-VOA-Quality-Flags": "occluded"}) for _ in range(4)
        ]
    assert responses[-1].json()["states"]["visible_obstacle_in_corridor"] != "warning"


def test_a_model_that_cannot_load_answers_503(workdir: Path) -> None:
    profile = workdir / "config" / "profiles" / "indoor_research_v0.yaml"
    text = profile.read_text(encoding="utf-8").replace("mock-detector", "broken-detector")
    profile.write_text(text, encoding="utf-8")
    manifest = workdir / "config" / "models" / "mock-detector.yaml"
    broken = manifest.read_text(encoding="utf-8").replace("mock-detector", "broken-detector")
    (workdir / "config" / "models" / "broken-detector.yaml").write_text(broken, encoding="utf-8")
    with make_client(workdir) as client:
        s = Session(client)
        with s.connect() as ws:
            next_of(ws, "auth_ok")
            assert s.upload(jpeg()).json() == {"code": "no_model_deployed"}


def test_frame_bytes_reach_no_file_db_or_log(
    client: TestClient, workdir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    s = Session(client)
    with s.connect() as ws:
        next_of(ws, "auth_ok")
        for _ in range(3):
            s.upload(jpeg(b"OBSTACLE " + CANARY))
    for path in workdir.rglob("*"):
        if path.is_file():
            assert CANARY not in path.read_bytes(), path.name
    for record in caplog.records:
        assert CANARY.decode() not in record.getMessage() + str(record.__dict__)
    db = sqlite3.connect(workdir / "var" / "voa.sqlite3")
    assert db.execute("SELECT count(*) FROM observations_meta").fetchone()[0] >= 3
