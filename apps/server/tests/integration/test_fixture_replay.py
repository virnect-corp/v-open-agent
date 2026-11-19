"""TASK-12 fixture parsing (SPEC 5) and UC-01..04 replay against expected decisions."""

import json
from pathlib import Path

import pytest

from voa.config.profile_loader import load_profile
from voa.contracts.decision import RiskDecision
from voa.contracts.observation import ModelRef
from voa.engine.engine import DecisionEngine
from voa.engine.ports import FixedEpochClock, SequentialIdFactory
from voa.engine.profile import PolicyProfile
from voa.providers.mock import MockVisionProvider
from voa.simulator.fixture import (
    FIXTURE_DEVICE_ID,
    FIXTURE_SESSION_ID,
    FixtureError,
    ReplayObservation,
    ReplayTick,
    decision_view,
    load_expected,
    load_fixture,
    parse_expected,
    parse_fixture,
)

REPO = Path(__file__).resolve().parents[4]
SCENARIOS = REPO / "contracts" / "fixtures" / "scenarios"
PROFILE_PATH = REPO / "apps" / "server" / "config" / "profiles" / "indoor_research_v0.yaml"
UC_FIXTURES = [
    "uc01_obstacle",
    "uc02_moving_person",
    "uc03_input_lost",
    "uc04_obstacle_cleared",
    "uc09_camera_down",
]
CLOCK = FixedEpochClock()

OBS_LINE = {
    "at_ms": 5,
    "obs": {
        "stream_id": "camera_0",
        "seq": 0,
        "capture_ts_monotonic_ns": 1,
        "model": {"id": "mock-detector", "revision": "0"},
        "payload": {"kind": "object_detection", "frame_id": "f0", "detections": []},
    },
}


def line(data: object) -> str:
    return json.dumps(data)


def replay(name: str) -> list[RiskDecision]:
    profile = load_profile(PROFILE_PATH)
    assert isinstance(profile, PolicyProfile)
    engine = DecisionEngine(
        FIXTURE_SESSION_ID, FIXTURE_DEVICE_ID, profile, SequentialIdFactory(), CLOCK
    )
    decisions: list[RiskDecision] = []
    for event in load_fixture(SCENARIOS / f"{name}.jsonl", CLOCK):
        if isinstance(event, ReplayObservation):
            result = engine.step(event.observation, event.now_mono_ns)
            assert result.admission == "accepted", (name, event.line_no)
            decisions += result.decisions
        else:
            decisions += engine.tick(event.now_mono_ns)
    return decisions


# --- UC-01..04 acceptance (INP-01) ---


@pytest.mark.parametrize("name", UC_FIXTURES)
def test_fixture_decisions_match_expected(name: str) -> None:
    expected = load_expected(SCENARIOS / f"{name}.expected.jsonl")
    assert expected, "an empty expectation would pass vacuously"
    assert [decision_view(d) for d in replay(name)] == expected


@pytest.mark.parametrize("name", UC_FIXTURES)
def test_fixture_replay_is_byte_identical(name: str) -> None:
    first = [d.model_dump_json() for d in replay(name)]
    assert first == [d.model_dump_json() for d in replay(name)]


def test_use_case_stories() -> None:
    """Each fixture demonstrates the story it is named after (PRD UC table)."""
    states = {name: [(d.previous_state, d.state) for d in replay(name)] for name in UC_FIXTURES}
    assert ("candidate", "warning") in states["uc01_obstacle"]
    assert states["uc02_moving_person"][-1] == ("warning", "unknown")
    assert states["uc03_input_lost"][-1] == ("warning", "unknown")
    assert ("warning", "resolved") not in states["uc03_input_lost"]
    assert states["uc04_obstacle_cleared"][-2:] == [("warning", "resolved"), ("resolved", "clear")]
    assert states["uc09_camera_down"][-1] == ("warning", "unknown")


def test_uc02_fixture_is_what_the_mock_provider_emits() -> None:
    """The moving-person fixture is reproducible from a MockVisionProvider frame script."""
    provider = MockVisionProvider()
    person = {
        "label": "person",
        "bbox": [0.3, 0.4, 0.7, 0.9],
        "score": 0.9,
        "track_id": "trk_0",
        "motion_class": "moving",
    }
    drafts = []
    for i, at_ms in enumerate([0, 100, 200, 300, 600, 1000, 1400, 1800]):
        drafts += provider.process(
            {
                "frame_id": f"f{i}",
                "capture_ts_monotonic_ns": at_ms * 1_000_000,
                "objects": [person],
                "track_flags": ["camera_motion_uncompensated"] if at_ms >= 300 else [],
            }
        )
    # The detector reports nothing for the person; the fixture keeps detections empty.
    events = load_fixture(SCENARIOS / "uc02_moving_person.jsonl", CLOCK)
    fixture_drafts = [e.observation for e in events if isinstance(e, ReplayObservation)]
    assert len(drafts) == len(fixture_drafts)
    for draft, observation in zip(drafts, fixture_drafts, strict=True):
        assert draft.seq == observation.seq
        assert draft.capture_ts_monotonic_ns == observation.capture_ts_monotonic_ns
        assert draft.quality_flags == observation.quality_flags
        assert draft.model == observation.model
        assert draft.payload.kind == observation.payload.kind
        if draft.payload.kind == "track_state":
            assert draft.payload == observation.payload


# --- Parser: success ---


def test_parser_fills_server_fields() -> None:
    lines = [
        line(OBS_LINE),
        line({"at_ms": 7, "tick": True}),
        line(
            {
                "at_ms": 7,
                "obs": {
                    **OBS_LINE["obs"],  # type: ignore[dict-item]
                    "seq": 1,
                    "clock_quality": "synced",
                    "model": {"id": "m", "revision": "2"},
                    "quality_flags": ["occluded"],
                },
            }
        ),
    ]
    first, tick, second = parse_fixture(lines, CLOCK)
    assert isinstance(first, ReplayObservation) and isinstance(second, ReplayObservation)
    assert isinstance(tick, ReplayTick)
    assert (first.line_no, tick.line_no, second.line_no) == (1, 2, 3)
    assert (first.now_mono_ns, tick.now_mono_ns) == (5_000_000, 7_000_000)

    obs = first.observation
    assert obs.observation_id == "obs_0000"
    assert second.observation.observation_id == "obs_0001"  # ticks do not consume numbers
    assert (obs.session_id, obs.device_id) == ("sess_fixture", "dev_fixture")
    assert obs.schema_version == "0.1.0"
    assert obs.server_received_mono_ns == 5_000_000
    assert obs.received_at_utc == CLOCK.at(5_000_000)
    assert obs.capture_ts_monotonic_ns == 1
    assert (obs.clock_quality, obs.quality_flags) == ("unknown", frozenset())
    assert obs.model == ModelRef(id="mock-detector", revision="0")
    assert second.observation.clock_quality == "synced"
    assert second.observation.model == ModelRef(id="m", revision="2")
    assert second.observation.quality_flags == frozenset({"occluded"})


def test_blank_lines_are_skipped_but_counted() -> None:
    events = parse_fixture(["", line(OBS_LINE), "   "], CLOCK)
    assert [e.line_no for e in events] == [2]


def test_expected_view_includes_alert_id_only_with_alert() -> None:
    rows = parse_expected(
        [
            line(
                {
                    "scenario_id": "s",
                    "previous_state": "unknown",
                    "state": "candidate",
                    "reason_codes": ["fresh_evidence_returned"],
                    "evidence_ids": ["obs_0000"],
                    "risk_event_id": "risk_0001",
                }
            )
        ]
    )
    assert "alert_id" not in rows[0]
    warning = replay("uc01_obstacle")[1]
    assert decision_view(warning)["alert_id"] == "alert_0001"


# --- Parser: failures, with line numbers (missing / out-of-order input) ---


@pytest.mark.parametrize(
    ("lines", "line_no"),
    [
        ([line(OBS_LINE), line({"at_ms": 4, "tick": True})], 2),  # at_ms decreased
        ([line({"at_ms": 0})], 1),  # neither obs nor tick
        (
            [line({"at_ms": 0, "obs": {k: v for k, v in OBS_LINE["obs"].items() if k != "model"}})],  # type: ignore[attr-defined]
            1,
        ),  # vision observation without model (ADR-0004)
        ([line({**OBS_LINE, "tick": True})], 1),  # both
        ([line({"at_ms": 0, "tick": False})], 1),
        ([line({"at_ms": -1, "tick": True})], 1),
        ([line({"at_ms": 0, "tick": True, "note": "x"})], 1),  # unknown key
        (["{not json"], 1),
        (["[1, 2]"], 1),
        (
            [
                line({"at_ms": 0, "tick": True}),
                line(
                    {
                        "at_ms": 1,
                        "obs": {
                            **OBS_LINE["obs"],  # type: ignore[dict-item]
                            "payload": {
                                "kind": "object_detection",
                                "frame_id": "f0",
                                "detections": [
                                    {"label": "x", "bbox": [0.9, 0.1, 0.1, 0.2], "score": 0.5}
                                ],
                            },
                        },
                    }
                ),
            ],
            2,
        ),  # schema violation inside obs
    ],
)
def test_parser_rejects_bad_lines_with_line_number(lines: list[str], line_no: int) -> None:
    with pytest.raises(FixtureError) as error:
        parse_fixture(lines, CLOCK)
    assert error.value.line_no == line_no
    assert f"line {line_no}" in str(error.value)


def test_parser_error_does_not_echo_payload_text() -> None:
    secret = "do-not-log-this-text"
    bad = line({"at_ms": 0, "tick": True, "note": secret})
    with pytest.raises(FixtureError) as error:
        parse_fixture([bad], CLOCK)
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "row",
    [
        {"scenario_id": "s"},  # missing fields
        {
            "scenario_id": "s",
            "previous_state": "unknown",
            "state": "bogus",
            "reason_codes": [],
            "evidence_ids": [],
            "risk_event_id": "none",
        },
    ],
)
def test_expected_parser_rejects_bad_rows(row: dict[str, object]) -> None:
    with pytest.raises(FixtureError) as error:
        parse_expected([line(row)])
    assert error.value.line_no == 1
