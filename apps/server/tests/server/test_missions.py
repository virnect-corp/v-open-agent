"""TASK-41 mission store and per-session mission selection (ADR-0010; ENG-15, ENG-19)."""

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from voa.config.missions import MissionStore, MissionVersion
from voa.server.app import create_app
from voa.server.settings import Settings

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
SECRET = "example-secret-change-me"
MS = 1_000_000
CAPS = {"inputs": ["camera_0"], "outputs": ["hud"]}


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


def make_client(workdir: Path, mono: FakeMono, **env: str) -> TestClient:
    settings = Settings.from_env({f"VOA_{k}": v for k, v in env.items()}, base_dir=workdir)
    return TestClient(create_app(lambda: settings, mono_ns=mono))


@pytest.fixture
def api(workdir: Path) -> Iterator[tuple[TestClient, FakeMono]]:
    mono = FakeMono()
    with make_client(workdir, mono) as client:
        yield client, mono


def session(client: TestClient, **extra: Any) -> Any:
    body = {"device_id": "dev_example", "secret": SECRET, "capabilities": CAPS, **extra}
    return client.post("/v1/sessions", json=body)


def bearer(created: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {created['token']}"}


def connect(client: TestClient, created: dict[str, Any]) -> WebSocketTestSession:
    return client.websocket_connect(f"/v1/streams/{created['session_id']}", headers=bearer(created))


def next_of(ws: WebSocketTestSession, *types: str) -> dict[str, Any]:
    while True:
        message = dict(ws.receive_json())
        if message["type"] in types:
            return message


def obstacle(seq: int, now: int) -> dict[str, Any]:
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


# --- store ---


def test_profiles_become_missions_v1_once(tmp_path: Path) -> None:
    store = MissionStore(tmp_path / "active", CONFIG / "models")
    first = store.import_profiles(CONFIG / "profiles")
    assert sorted(first) == ["fire_c1", "indoor_research_v0", "local_verify_c0"]
    assert store.import_profiles(CONFIG / "profiles") == []  # idempotent
    fire = store.published("fire_c1")
    assert isinstance(fire, MissionVersion)
    assert (fire.version, fire.profile.profile_id, fire.profile.profile_version) == (
        1,
        "fire_c1",
        1,
    )
    assert [m.mission_id for m in store.all()] == [
        "fire_c1",
        "indoor_research_v0",
        "local_verify_c0",
    ]


def test_archived_unknown_or_tampered_missions_are_not_offered(tmp_path: Path) -> None:
    store = MissionStore(tmp_path / "active", CONFIG / "models")
    store.import_profiles(CONFIG / "profiles")
    store.set_status("local_verify_c0", "archived")
    assert store.published("local_verify_c0") is None
    assert store.published("nope") is None and store.published("../etc") is None
    v1 = store.root / "fire_c1" / "v1.yaml"
    data = yaml.safe_load(v1.read_text(encoding="utf-8"))
    data["profile_id"] = "other"
    v1.write_text(yaml.safe_dump(data), encoding="utf-8")
    broken = store.published("fire_c1")
    assert broken is not None and not isinstance(broken, MissionVersion)  # ProfileError


# --- sessions ---


def test_default_mission_is_the_policy_profile_and_decisions_carry_its_version(
    api: tuple[TestClient, FakeMono],
) -> None:
    client, mono = api
    created = session(client).json()
    assert created["mission"] == {
        "mission_id": "indoor_research_v0",
        "version": 1,
        "name": "indoor_research_v0",
        "frame_interval_ms": 333,
    }
    with connect(client, created) as ws:
        next_of(ws, "auth_ok")
        assert next_of(ws, "mission")["mission_id"] == "indoor_research_v0"
        mono.now += 100 * MS
        ws.send_json(obstacle(1, mono.now))
        decision = next_of(ws, "decision")["decision"]
    assert decision["policy_version"] == "indoor_research_v0@1"


def test_missions_list_needs_a_session_token_and_hides_archived(
    api: tuple[TestClient, FakeMono], workdir: Path
) -> None:
    client, _ = api
    assert client.get("/v1/missions").status_code == 401
    created = session(client).json()
    MissionStore(workdir / "var" / "config", workdir / "config" / "models").set_status(
        "local_verify_c0", "archived"
    )
    listed = client.get("/v1/missions", headers=bearer(created)).json()["missions"]
    assert [m["mission_id"] for m in listed] == ["fire_c1", "indoor_research_v0"]
    fire = listed[0]
    assert fire["version"] == 1 and fire["models"] == ["rtdetr-r18-dfire"]
    assert fire["scenarios"] == [
        {"id": "fire_in_view", "labels": ["fire"], "severity": "attention"}
    ]


def test_session_can_name_its_mission_or_fails_for_unknown_ones(
    api: tuple[TestClient, FakeMono],
) -> None:
    client, _ = api
    fire = session(client, mission_id="fire_c1")
    assert fire.status_code == 200 and fire.json()["mission"]["mission_id"] == "fire_c1"
    assert session(client, mission_id="nope").json() == {"code": "mission_not_found"}


def test_switching_restarts_every_scenario_from_unknown(api: tuple[TestClient, FakeMono]) -> None:
    client, mono = api
    created = session(client).json()
    with connect(client, created) as ws:
        next_of(ws, "auth_ok")
        for seq in range(1, 4):
            mono.now += 100 * MS
            ws.send_json(obstacle(seq, mono.now))
        next_of(ws, "alert")
        switched = client.put(
            f"/v1/sessions/{created['session_id']}/mission",
            json={"mission_id": "fire_c1"},
            headers=bearer(created),
        )
        assert switched.json() == {
            "mission_id": "fire_c1",
            "version": 1,
            "name": "fire_c1",
            "frame_interval_ms": 333,
        }
        assert next_of(ws, "mission")["mission_id"] == "fire_c1"
        beat = next_of(ws, "heartbeat")
    assert beat["states"] == {"fire_in_view": "unknown"}


def test_switch_rejects_unknown_missions_and_other_sessions(
    api: tuple[TestClient, FakeMono],
) -> None:
    client, _ = api
    a, b = session(client).json(), session(client).json()
    path = f"/v1/sessions/{a['session_id']}/mission"
    assert client.put(path, json={"mission_id": "fire_c1"}).status_code == 401
    assert client.put(path, json={"mission_id": "fire_c1"}, headers=bearer(b)).status_code == 404
    unknown = client.put(path, json={"mission_id": "nope"}, headers=bearer(a))
    assert unknown.json() == {"code": "mission_not_found"}


def test_without_a_default_the_session_waits_for_a_mission(workdir: Path) -> None:
    mono = FakeMono()
    with make_client(workdir, mono, DEFAULT_MISSION="-") as client:
        created = session(client).json()
        assert created["mission"] is None
        with connect(client, created) as ws:
            next_of(ws, "auth_ok")
            ws.send_json(obstacle(1, mono.now))
            assert next_of(ws, "error")["code"] == "mission_not_selected"
            assert next_of(ws, "heartbeat")["states"] == {}
            client.put(
                f"/v1/sessions/{created['session_id']}/mission",
                json={"mission_id": "indoor_research_v0"},
                headers=bearer(created),
            )
            assert next_of(ws, "mission")["version"] == 1
            mono.now += 100 * MS
            ws.send_json(obstacle(2, mono.now))
            assert next_of(ws, "decision")["decision"]["scenario_id"]


def test_a_missing_default_mission_makes_the_server_not_ready(workdir: Path) -> None:
    with make_client(workdir, FakeMono(), DEFAULT_MISSION="nope") as client:
        ready = client.get("/v1/readyz")
    assert ready.status_code == 503 and ready.json()["error_code"] == "profile_invalid"
