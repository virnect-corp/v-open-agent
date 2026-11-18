"""TASK-24 model provenance (SPEC 3.3, ADR-0004) and profile required_models (SPEC 4.9)."""

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.builders import build_observation
from voa.config.profile_loader import load_profile
from voa.contracts.observation import (
    DetectionPayload,
    HealthPayload,
    ModelRef,
    Observation,
    ObservationDraft,
    TrackPayload,
    model_id_of,
)
from voa.engine.profile import PolicyProfile

PROFILES = Path(__file__).resolve().parents[1] / "fixtures" / "profiles"
DEFAULT_PROFILE = Path(__file__).resolve().parents[2] / "config" / "profiles"
DETECTION = {"kind": "object_detection", "frame_id": "f0", "detections": []}
TRACK = {"kind": "track_state", "frame_id": "f0", "tracks": []}
HEALTH = {"kind": "health", "status": "ok", "detail_code": "x"}
SOUND = {
    "kind": "sound_event",
    "chunk_id": "c0",
    "window_start_ns": 0,
    "window_end_ns": 1,
    "events": [],
}


def draft(payload: dict[str, Any], model: dict[str, str] | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "stream_id": "camera_0",
        "seq": 0,
        "capture_ts_monotonic_ns": 0,
        "payload": payload,
    }
    if model is not None:
        data["model"] = model
    return data


# --- Contract: vision observations must name their model ---


@pytest.mark.parametrize("payload", [DETECTION, TRACK])
def test_vision_draft_without_model_is_a_schema_error(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="model is required"):
        ObservationDraft.model_validate(draft(payload, None))
    with pytest.raises(ValidationError, match="model is required"):
        ObservationDraft.model_validate({**draft(payload, None), "model": None})


@pytest.mark.parametrize("payload", [DETECTION, TRACK])
def test_vision_observation_without_model_is_a_schema_error(payload: dict[str, Any]) -> None:
    observation = build_observation(
        model=ModelRef(id="m", revision="0"),
        payload=DetectionPayload.model_validate(payload)
        if payload["kind"] == "object_detection"
        else TrackPayload.model_validate(payload),
    )
    data = observation.model_dump()
    data["model"] = None
    with pytest.raises(ValidationError, match="model is required"):
        Observation.model_validate(data)


@pytest.mark.parametrize("payload", [HEALTH, SOUND])
def test_non_vision_kinds_may_omit_model(payload: dict[str, Any]) -> None:
    assert ObservationDraft.model_validate(draft(payload, None)).model is None


def test_vision_draft_with_model_is_accepted() -> None:
    parsed = ObservationDraft.model_validate(draft(TRACK, {"id": "mock-tracker", "revision": "0"}))
    assert parsed.model == ModelRef(id="mock-tracker", revision="0")


def test_schema_documents_the_rule_for_other_clients() -> None:
    for model in (ObservationDraft, Observation):
        description = model.model_json_schema()["properties"]["model"]["description"]
        assert "object_detection" in description and "track_state" in description


def test_model_key_is_model_id_or_empty() -> None:
    assert model_id_of(build_observation(model=ModelRef(id="crack-det", revision="3"))) == (
        "crack-det"
    )
    health = build_observation(payload=HealthPayload(kind="health", status="ok", detail_code="x"))
    assert health.model is None and model_id_of(health) == ""


def test_builder_fills_mock_models_for_vision_payloads() -> None:
    detection = build_observation()
    track = build_observation(payload=TrackPayload(kind="track_state", frame_id="f", tracks=[]))
    assert detection.model == ModelRef(id="mock-detector", revision="0")
    assert track.model == ModelRef(id="mock-tracker", revision="0")


# --- Profile: required_models ---


def profile_data() -> dict[str, Any]:
    profile = load_profile(PROFILES / "valid.yaml")
    assert isinstance(profile, PolicyProfile)
    return copy.deepcopy(profile.model_dump(mode="json"))


def test_default_and_test_profiles_declare_mock_models() -> None:
    for path in (PROFILES / "valid.yaml", DEFAULT_PROFILE / "indoor_research_v0.yaml"):
        profile = load_profile(path)
        assert isinstance(profile, PolicyProfile)
        moving, obstacle = profile.scenarios
        assert moving.required_models == {
            "object_detection": "mock-detector",
            "track_state": "mock-tracker",
        }
        assert obstacle.required_models == {"object_detection": "mock-detector"}


@pytest.mark.parametrize(
    ("index", "models"),
    [
        (1, {}),  # nothing declared
        (0, {"object_detection": "mock-detector"}),  # track kind missing
        (1, {"object_detection": "mock-detector", "track_state": "mock-tracker"}),  # extra kind
        (1, {"track_state": "mock-tracker"}),  # wrong kind
        (1, {"object_detection": ""}),  # empty model id
        (1, {"object_detection": "bad id!"}),  # Id rule
    ],
)
def test_required_models_must_cover_exactly_the_required_kinds(
    index: int, models: dict[str, str]
) -> None:
    data = profile_data()
    data["scenarios"][index]["required_models"] = models
    with pytest.raises(ValidationError):
        PolicyProfile.model_validate(data)


def test_required_models_is_mandatory() -> None:
    data = profile_data()
    del data["scenarios"][1]["required_models"]
    with pytest.raises(ValidationError):
        PolicyProfile.model_validate(data)


def test_loader_reports_profile_invalid_without_echoing_values(tmp_path: Path) -> None:
    source = (PROFILES / "valid.yaml").read_text(encoding="utf-8")
    bad = source.replace(
        "required_models: {object_detection: mock-detector}\n",
        "required_models: {object_detection: secret-model-name, extra: x}\n",
        1,
    )
    assert bad != source
    path = tmp_path / "bad.yaml"
    path.write_text(bad, encoding="utf-8")
    error = load_profile(path)
    assert not isinstance(error, PolicyProfile)
    assert error.code == "profile_invalid"
    assert "secret-model-name" not in error.detail
