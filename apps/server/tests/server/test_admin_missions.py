"""TASK-44A admin mission API: draft, validate, publish, versions, archive, changes
(SPEC 7.7, ADR-0010; INP-05, ENG-15)."""

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.server.test_frames import (  # noqa: F401
    CAPS,
    SECRET,
    Session,
    make_client,
    next_of,
    workdir,
)
from tests.server.test_missions import obstacle

ADMIN_PASSWORD = "admin-pass-1"


@pytest.fixture
def client(workdir: Path) -> Iterator[TestClient]:  # noqa: F811
    with make_client(workdir) as c:
        yield c


def admin_token(client: TestClient) -> str:
    first = client.post("/v1/auth/login", json={"username": "admin", "password": "admin"}).json()
    client.post(
        "/v1/auth/password",
        json={"current_password": "admin", "new_password": ADMIN_PASSWORD},
        headers={"Authorization": f"Bearer {first['token']}"},
    )
    login = {"username": "admin", "password": ADMIN_PASSWORD}
    return str(client.post("/v1/auth/login", json=login).json()["token"])


class Admin:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {admin_token(client)}"}

    def get(self, path: str) -> Any:
        return self.client.get(f"/v1/admin{path}", headers=self.headers)

    def post(self, path: str, body: Any = None) -> Any:
        return self.client.post(f"/v1/admin{path}", json=body, headers=self.headers)

    def put(self, path: str, body: Any) -> Any:
        return self.client.put(f"/v1/admin{path}", json=body, headers=self.headers)

    def delete(self, path: str) -> Any:
        return self.client.delete(f"/v1/admin{path}", headers=self.headers)


@pytest.fixture
def admin(client: TestClient) -> Admin:
    return Admin(client)


def indoor_profile(workdir: Path, **params: Any) -> dict[str, Any]:  # noqa: F811
    path = workdir / "config" / "profiles" / "indoor_research_v0.yaml"
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["params"].update(params)
    return data


def policy_versions(session: Session, frames: int = 3) -> set[str]:
    """Send obstacle observations over the control socket and collect policy versions."""
    seen = set()
    with session.connect() as ws:
        next_of(ws, "auth_ok")
        for seq in range(1, frames + 1):
            ws.send_json(obstacle(seq, time.monotonic_ns()))
            seen.add(next_of(ws, "decision")["decision"]["policy_version"])
    return seen


# --- access ---


def test_admin_mission_routes_need_an_admin_login(client: TestClient) -> None:
    device = Session(client)
    for headers in ({}, device.auth, {"Authorization": "Bearer nope"}):
        assert client.get("/v1/admin/missions", headers=headers).status_code == 401
        assert client.post("/v1/admin/missions/fire_c1:publish", headers=headers).status_code == 401
        assert client.get("/v1/admin/changes", headers=headers).status_code == 401


# --- create, draft, validate, publish ---


def test_a_new_mission_is_drafted_validated_published_and_offered(
    admin: Admin,
    client: TestClient,
    workdir: Path,  # noqa: F811
) -> None:
    created = admin.post(
        "/missions", {"mission_id": "corridor_b", "name": "복도 B", "description": "2층"}
    )
    assert created.status_code == 201
    assert created.json()["status"] == "draft" and created.json()["latest_version"] == 0
    assert admin.post("/missions/corridor_b:validate").json() == {"code": "draft_missing"}

    profile = {**indoor_profile(workdir), "profile_id": "corridor_b"}
    settings = {"frame_interval_ms": 500, "hud_style_map": {"attention": "attention_default"}}
    assert admin.put("/missions/corridor_b", {"profile": profile, "settings": settings}).json()[
        "has_draft"
    ]
    assert admin.post("/missions/corridor_b:validate").json() == {"valid": True, "next_version": 1}
    published = admin.post("/missions/corridor_b:publish").json()
    assert published == {"mission_id": "corridor_b", "version": 1, "status": "published"}

    detail = admin.get("/missions/corridor_b").json()
    assert detail["draft"] is None and detail["published"]["version"] == 1
    assert detail["published"]["settings"] == settings

    device = Session(client, mission_id="corridor_b")
    assert device.created["frame_interval_ms"] == 500
    assert device.created["mission"] == {
        "mission_id": "corridor_b",
        "version": 1,
        "name": "복도 B",
        "frame_interval_ms": 500,
    }
    offered = client.get("/v1/missions", headers=device.auth).json()["missions"]
    assert {"corridor_b": 500} == {
        m["mission_id"]: m["frame_interval_ms"] for m in offered if m["mission_id"] == "corridor_b"
    }
    with device.connect() as ws:
        next_of(ws, "auth_ok")
        assert next_of(ws, "mission")["frame_interval_ms"] == 500


def test_a_mission_can_be_copied_from_another(admin: Admin) -> None:
    copied = admin.post(
        "/missions", {"mission_id": "fire_copy", "name": "화재 2", "from_mission_id": "fire_c1"}
    )
    assert copied.status_code == 201 and copied.json()["has_draft"]
    draft = admin.get("/missions/fire_copy").json()["draft"]
    assert draft["profile"]["profile_id"] == "fire_copy"
    assert "profile_version" not in draft["profile"]
    assert admin.post("/missions/fire_copy:validate").json()["valid"]


def test_create_rejects_bad_ids_duplicates_and_unknown_sources(admin: Admin) -> None:
    assert admin.post("/missions", {"mission_id": "Bad Id", "name": "x"}).json() == {
        "code": "invalid_request"
    }
    assert admin.post("/missions", {"mission_id": "fire_c1", "name": "x"}).status_code == 409
    unknown = admin.post(
        "/missions", {"mission_id": "new_one", "name": "x", "from_mission_id": "nope"}
    )
    assert unknown.status_code == 404 and unknown.json() == {"code": "mission_not_found"}
    assert admin.get("/missions/nope").status_code == 404
    assert admin.get("/missions/..").status_code == 404


def test_bad_settings_are_rejected_before_they_reach_the_draft(admin: Admin) -> None:
    too_fast = admin.put("/missions/fire_c1", {"settings": {"frame_interval_ms": 50}})
    bad_key = admin.put("/missions/fire_c1", {"settings": {"hud_style_map": {"a b": "x"}}})
    assert too_fast.json() == bad_key.json() == {"code": "invalid_request"}
    assert admin.get("/missions/fire_c1").json()["draft"] is None


# --- versions and running sessions ---


def test_publishing_bumps_the_version_and_running_sessions_keep_theirs(
    admin: Admin,
    client: TestClient,
    workdir: Path,  # noqa: F811
) -> None:
    before = Session(client)  # default mission indoor_research_v0 v1
    edited = indoor_profile(workdir, confirm_frames=2)
    assert admin.put("/missions/indoor_research_v0", {"profile": edited}).status_code == 200
    assert admin.post("/missions/indoor_research_v0:publish").json()["version"] == 2

    after = Session(client)
    assert after.created["mission"]["version"] == 2
    assert policy_versions(before) == {"indoor_research_v0@1"}
    assert policy_versions(after) == {"indoor_research_v0@2"}
    versions = admin.get("/missions/indoor_research_v0/versions").json()["versions"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["profile"]["params"]["confirm_frames"] == 2


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"params": {"min_score": 2}}, "profile_invalid"),
        ({"profile_id": "someone_else"}, "profile_invalid"),
        ({"unknown_field": 1}, "profile_invalid"),
        ({"models": "mock-detector-missing"}, "profile_model_mismatch"),
    ],
)
def test_a_failed_publish_keeps_the_published_version(
    admin: Admin,
    workdir: Path,  # noqa: F811
    change: dict[str, Any],
    code: str,
) -> None:
    profile = indoor_profile(workdir)
    if "models" in change:
        for scenario in profile["scenarios"]:
            if "object_detection" in scenario["required_models"]:
                scenario["required_models"]["object_detection"] = change["models"]
    elif "params" in change:
        profile["params"].update(change["params"])
    else:
        profile.update(change)
    admin.put("/missions/indoor_research_v0", {"profile": profile})
    failed = admin.post("/missions/indoor_research_v0:publish")
    assert failed.status_code == 400 and failed.json() == {"code": code}
    detail = admin.get("/missions/indoor_research_v0").json()
    assert detail["latest_version"] == 1 and detail["published"]["version"] == 1
    assert detail["draft"] is not None  # kept for fixing
    [last] = admin.get("/changes").json()["changes"][:1]
    assert (last["action"], last["result_code"], last["to_version"]) == (
        "mission_publish",
        code,
        None,
    )


def test_publish_refuses_a_model_whose_weights_are_not_ready(
    workdir: Path,  # noqa: F811
) -> None:
    manifest = workdir / "config" / "models" / "mock-detector.yaml"
    broken = manifest.read_text(encoding="utf-8").replace("mock-detector", "broken-detector")
    (workdir / "config" / "models" / "broken-detector.yaml").write_text(broken, encoding="utf-8")
    with make_client(workdir) as client:
        admin = Admin(client)
        profile = indoor_profile(workdir)
        for scenario in profile["scenarios"]:
            if "object_detection" in scenario["required_models"]:
                scenario["required_models"]["object_detection"] = "broken-detector"
        admin.put("/missions/indoor_research_v0", {"profile": profile})
        assert admin.post("/missions/indoor_research_v0:validate").json()["valid"]
        refused = admin.post("/missions/indoor_research_v0:publish")
        assert refused.json() == {"code": "model_not_ready"}
        assert admin.get("/missions/indoor_research_v0").json()["latest_version"] == 1


# --- archive ---


def test_a_mission_in_use_cannot_be_archived(admin: Admin, client: TestClient) -> None:
    device = Session(client, mission_id="fire_c1")
    assert admin.delete("/missions/fire_c1").json() == {"code": "mission_in_use"}
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    portal = client.portal
    assert portal is not None
    portal.call(runtime.end_session, device.id, runtime.mono_ns())
    archived = admin.delete("/missions/fire_c1")
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    other = Session(client)
    offered = client.get("/v1/missions", headers=other.auth).json()["missions"]
    assert "fire_c1" not in [m["mission_id"] for m in offered]
    body = {
        "device_id": "dev_example",
        "secret": SECRET,
        "capabilities": CAPS,
        "mission_id": "fire_c1",
    }
    assert client.post("/v1/sessions", json=body).json() == {"code": "mission_not_found"}


# --- change log ---


def test_changes_record_actions_and_outcomes_but_no_contents_or_tokens(
    admin: Admin,
    workdir: Path,  # noqa: F811
) -> None:
    marker = "MARKER-NOTICE-TEXT-7c2"
    profile = {**indoor_profile(workdir), "degraded_notice_text": marker}
    admin.put("/missions/indoor_research_v0", {"profile": profile, "name": "실내"})
    admin.post("/missions/indoor_research_v0:publish")
    admin.post("/missions", {"mission_id": "fire_c1", "name": "dup"})
    changes = admin.get("/changes").json()["changes"]
    summary = [(c["action"], c["target"], c["result_code"]) for c in changes]
    assert summary == [
        ("mission_create", "fire_c1", "mission_exists"),
        ("mission_publish", "indoor_research_v0", "ok"),
        ("mission_update", "indoor_research_v0", "ok"),
    ]
    assert (changes[1]["from_version"], changes[1]["to_version"]) == (1, 2)
    assert set(changes[0]) == {
        "id",
        "changed_at_utc",
        "action",
        "target",
        "from_version",
        "to_version",
        "result_code",
    }
    token = admin.headers["Authorization"].split()[1]
    db = (workdir / "var" / "voa.sqlite3").read_bytes()
    assert marker.encode() not in db and token.encode() not in db
