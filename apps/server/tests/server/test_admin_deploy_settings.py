"""TASK-44B models, deployments, settings, HUD presentation and one-image detect
(SPEC 7.4, 7.7; ADR-0009; INP-05)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.server.test_admin_missions import Admin, admin_token  # noqa: F401
from tests.server.test_frames import Session, jpeg, make_client, next_of, workdir  # noqa: F401
from voa.config.missions import MissionSettings, MissionVersion
from voa.server.hud import ATTENTION_DEFAULT, DEFAULT_STYLES, DEGRADED_DEFAULT, choose_style

CALM = {
    "position": "bottom_center",
    "text_size": "normal",
    "color": "white",
    "flash": {"mode": "none", "count": 0, "on_ms": 0, "off_ms": 0},
    "show_ms": 2000,
    "speech": False,
    "haptic": "none",
}


@pytest.fixture
def client(workdir: Path) -> Iterator[TestClient]:  # noqa: F811
    with make_client(workdir) as c:
        yield c


@pytest.fixture
def admin(client: TestClient) -> Admin:
    return Admin(client)


def models(admin: Admin) -> dict[str, dict[str, Any]]:
    return {m["model_id"]: m for m in admin.get("/models").json()["models"]}


def alert_presentation(client: TestClient) -> dict[str, Any]:
    s = Session(client)
    with s.connect() as ws:
        next_of(ws, "auth_ok")
        for _ in range(3):
            s.upload(jpeg(b"OBSTACLE"))
        presentation: dict[str, Any] = next_of(ws, "alert")["presentation"]
    return presentation


def test_new_admin_routes_need_an_admin_login(client: TestClient) -> None:
    device = Session(client)
    for headers in ({}, device.auth):
        assert client.get("/v1/admin/models", headers=headers).status_code == 401
        assert client.get("/v1/admin/settings", headers=headers).status_code == 401
        assert (
            client.post(
                "/v1/admin/deployments", json={"model_id": "x"}, headers=headers
            ).status_code
            == 401
        )
        assert client.post("/v1/admin/detect?model_id=x", headers=headers).status_code == 401


# --- models and deployments ---


def test_models_report_weights_deployment_and_use(
    admin: Admin,
    workdir: Path,  # noqa: F811
) -> None:
    bad = workdir / "var" / "models" / "rtdetr-r18-coco"
    bad.mkdir(parents=True)
    (bad / "model.safetensors").write_bytes(b"not the real weights")
    listed = models(admin)
    assert listed["mock-detector"]["weights"] == "ready"
    assert listed["rtdetr-r18-dfire"]["weights"] == "missing"
    assert listed["rtdetr-r18-coco"]["weights"] == "sha256_mismatch"
    assert listed["rtdetr-r18-dfire"]["used_by"] == ["fire_c1"]
    assert not listed["mock-detector"]["deployed"] and listed["mock-detector"]["device"] is None


def test_several_models_can_be_deployed_with_their_own_floor(admin: Admin) -> None:
    first = admin.post("/deployments", {"model_id": "mock-detector", "score_floor": 0.3})
    second = admin.post("/deployments", {"model_id": "rtdetr-r18-dfire"})
    assert first.status_code == second.status_code == 201
    listed = models(admin)
    assert (listed["mock-detector"]["deployed"], listed["mock-detector"]["score_floor"]) == (
        True,
        0.3,
    )
    assert (listed["rtdetr-r18-dfire"]["device"], listed["rtdetr-r18-dfire"]["score_floor"]) == (
        "cpu",
        0.25,
    )


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"model_id": "nope"}, 404, "model_not_found"),
        ({"model_id": "mock-tracker"}, 400, "model_not_servable"),
        ({"model_id": "mock-detector", "device": "cuda"}, 400, "device_unavailable"),
        ({"model_id": "mock-detector", "score_floor": 1.5}, 400, "invalid_request"),
    ],
)
def test_bad_deployments_are_refused(
    admin: Admin, body: dict[str, Any], status: int, code: str
) -> None:
    response = admin.post("/deployments", body)
    assert response.status_code == status and response.json() == {"code": code}


def test_a_model_a_published_mission_needs_cannot_be_undeployed(admin: Admin) -> None:
    admin.post("/deployments", {"model_id": "mock-detector"})
    admin.post("/deployments", {"model_id": "rtdetr-r18-dfire"})
    assert admin.delete("/deployments/mock-detector").json() == {"code": "model_in_use"}
    assert admin.delete("/deployments/rtdetr-r18-dfire").json() == {"code": "model_in_use"}
    assert admin.delete("/missions/fire_c1").status_code == 200  # archive its only user
    assert admin.delete("/deployments/rtdetr-r18-dfire").json() == {
        "model_id": "rtdetr-r18-dfire",
        "deployed": False,
    }
    assert admin.delete("/deployments/rtdetr-r18-dfire").status_code == 404
    actions = [(c["action"], c["result_code"]) for c in admin.get("/changes").json()["changes"]]
    assert ("model_undeploy", "model_in_use") in actions and ("model_undeploy", "ok") in actions


# --- settings and frame interval ---


def test_frame_interval_changes_reach_running_sessions_as_config(
    admin: Admin,
    client: TestClient,
    workdir: Path,  # noqa: F811
) -> None:
    defaults = admin.get("/settings").json()
    assert defaults["frame_interval_ms"] == 333
    assert {ATTENTION_DEFAULT, DEGRADED_DEFAULT} <= set(defaults["styles"])
    s = Session(client)
    with s.connect() as ws:
        next_of(ws, "auth_ok")
        assert admin.put("/settings", {"frame_interval_ms": 500}).status_code == 200
        assert next_of(ws, "config") == {"type": "config", "frame_interval_ms": 500}
    assert Session(client).created["frame_interval_ms"] == 500
    with make_client(workdir) as again:  # persisted under VOA_ACTIVE_CONFIG_DIR
        assert Admin(again).get("/settings").json()["frame_interval_ms"] == 500


def test_settings_reject_bad_values_and_unknown_styles(admin: Admin) -> None:
    assert admin.put("/settings", {"frame_interval_ms": 50}).json() == {"code": "invalid_request"}
    assert admin.put("/settings", {"hud_style_map": {"attention": "nope"}}).json() == {
        "code": "style_not_found"
    }
    assert admin.get("/settings").json()["hud_style_map"] == {}


def test_a_broken_settings_file_answers_503_until_saved(
    workdir: Path,  # noqa: F811
) -> None:
    path = workdir / "var" / "config" / "settings.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("frame_interval_ms: [", encoding="utf-8")
    with make_client(workdir) as client:
        admin = Admin(client)
        assert admin.get("/settings").json() == {"code": "settings_invalid"}
        assert Session(client).created["frame_interval_ms"] == 333  # defaults meanwhile
        assert admin.put("/settings", {"frame_interval_ms": 400}).status_code == 200
        assert admin.get("/settings").json()["frame_interval_ms"] == 400


# --- HUD presentation ---


def test_alerts_carry_the_default_presentation(client: TestClient) -> None:
    presentation = alert_presentation(client)
    assert presentation["style_id"] == ATTENTION_DEFAULT
    assert (presentation["color"], presentation["position"], presentation["text_size"]) == (
        "red",
        "top_center",
        "large",
    )
    assert presentation["flash"] == {"mode": "full_screen", "count": 3, "on_ms": 200, "off_ms": 200}
    assert presentation["speech"] and presentation["haptic"] == "pattern_a"
    assert presentation["text"]


def test_the_admin_map_picks_the_style_of_the_next_alert(admin: Admin, client: TestClient) -> None:
    saved = admin.put(
        "/settings", {"hud_styles": {"calm": CALM}, "hud_style_map": {"attention": "calm"}}
    )
    assert saved.status_code == 200
    presentation = alert_presentation(client)
    assert presentation["style_id"] == "calm" and presentation["color"] == "white"


def test_style_choice_mission_before_admin_and_scenario_before_severity(
    client: TestClient,
) -> None:
    style = DEFAULT_STYLES[ATTENTION_DEFAULT]
    styles = {name: style for name in (ATTENTION_DEFAULT, DEGRADED_DEFAULT, "a", "b", "c")}
    keys = ("fire_in_view", "attention")

    def choose(mission_map: dict[str, str], admin_map: dict[str, str]) -> str:
        return choose_style(keys, mission_map, admin_map, styles, ATTENTION_DEFAULT)

    assert choose({}, {}) == ATTENTION_DEFAULT
    assert choose({}, {"attention": "a"}) == "a"
    assert choose({}, {"attention": "a", "fire_in_view": "b"}) == "b"
    assert choose({"attention": "c"}, {"fire_in_view": "b"}) == "c"
    assert choose({"fire_in_view": "missing"}, {"attention": "a"}) == "a"  # stale entry skipped

    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    mission = runtime.missions.published("indoor_research_v0")
    assert isinstance(mission, MissionVersion)
    notice = runtime.presentation(mission, "notice", "x", "판단 불가")
    assert notice.style_id == DEGRADED_DEFAULT and notice.color == "yellow"
    styled = MissionVersion(
        mission.info,
        mission.version,
        mission.profile,
        MissionSettings(hud_style_map={"degraded": ATTENTION_DEFAULT}),
    )
    assert runtime.presentation(styled, "notice", "x", "판단 불가").style_id == ATTENTION_DEFAULT


# --- one-image detect ---


def test_detect_returns_detections_without_storing_the_image(
    admin: Admin,
    workdir: Path,  # noqa: F811
) -> None:
    canary = b"CANARY-DETECT-91fe"
    path = "/detect?model_id=mock-detector"
    headers = {**admin.headers, "Content-Type": "image/jpeg"}

    def post(data: bytes) -> Any:
        return admin.client.post(f"/v1/admin{path}", content=data, headers=headers)

    assert post(jpeg(b"OBSTACLE")).json() == {"code": "no_model_deployed"}
    admin.post("/deployments", {"model_id": "mock-detector"})
    found = post(jpeg(b"OBSTACLE " + canary)).json()
    assert found["model_id"] == "mock-detector" and (found["width"], found["height"]) == (640, 360)
    assert [d["label"] for d in found["detections"]] == ["obstacle"]
    assert post(b"not a jpeg").json() == {"code": "frame_rejected"}
    for file in workdir.rglob("*"):
        if file.is_file():
            assert canary not in file.read_bytes(), file.name


def test_detect_uses_the_deployment_score_floor(admin: Admin) -> None:
    admin.post("/deployments", {"model_id": "mock-detector", "score_floor": 0.95})
    headers = {**admin.headers, "Content-Type": "image/jpeg"}
    found = admin.client.post(
        "/v1/admin/detect?model_id=mock-detector", content=jpeg(b"OBSTACLE"), headers=headers
    ).json()
    assert found["detections"] == []  # the fake detection scores 0.9
