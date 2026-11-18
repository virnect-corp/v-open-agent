"""TASK-25 one frame analysed by several models (SPEC 4.2~4.4, ADR-0004, T-R19)."""

from pathlib import Path

import pytest

from tests.builders import build_observation
from voa.config.profile_loader import load_profile
from voa.contracts.observation import (
    Detection,
    DetectionPayload,
    ModelRef,
    Observation,
    Track,
    TrackPayload,
)
from voa.engine.engine import DecisionEngine, StepResult
from voa.engine.ports import FixedEpochClock, SequentialIdFactory
from voa.engine.profile import PolicyProfile
from voa.replay.__main__ import main as replay_main
from voa.simulator.fixture import decision_view, load_expected

MS = 1_000_000
CRACK, FIRE = "crack_on_surface", "fire_in_view"
MOVING, OBSTACLE = "moving_object_in_attention_zone", "visible_obstacle_in_corridor"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "profiles"
SCENARIOS = Path(__file__).resolve().parents[4] / "contracts" / "fixtures" / "scenarios"
TR19 = SCENARIOS / "tr19_two_models_one_stops"


def profile(name: str, **params: int) -> PolicyProfile:
    loaded = load_profile(FIXTURES / name)
    assert isinstance(loaded, PolicyProfile)
    if params:
        loaded = loaded.model_copy(update={"params": loaded.params.model_copy(update=params)})
    return loaded


def engine_for(loaded: PolicyProfile) -> DecisionEngine:
    return DecisionEngine("sess_0001", "dev_0001", loaded, SequentialIdFactory(), FixedEpochClock())


class Camera:
    """Emit detections for one frame from named models; seq counts per model."""

    def __init__(self, engine: DecisionEngine) -> None:
        self.engine = engine
        self.count = 0
        self.seq: dict[str, int] = {}

    def obs(self, model: str, now: int, frame: str, labels: list[str]) -> Observation:
        self.count += 1
        seq = self.seq.get(model, -1) + 1
        self.seq[model] = seq
        return build_observation(
            observation_id=f"obs_{self.count:04d}",
            seq=seq,
            capture_ts_monotonic_ns=now,
            server_received_mono_ns=now,
            model=ModelRef(id=model, revision="0"),
            payload=DetectionPayload(
                kind="object_detection",
                frame_id=frame,
                detections=[
                    Detection(label=label, bbox=(0.2, 0.3, 0.6, 0.5), score=0.9) for label in labels
                ],
            ),
        )

    def step(self, model: str, now: int, frame: str, labels: list[str]) -> StepResult:
        return self.engine.step(self.obs(model, now, frame, labels), now)


# --- Success: both models' observations of one frame are accepted and kept apart ---


def test_same_frame_from_two_models_is_accepted_for_both() -> None:
    camera = Camera(engine_for(profile("two_models.yaml")))
    crack = camera.step("crack-detector", 0, "f0", ["crack"])
    fire = camera.step("fire-detector", 0, "f0", [])
    assert (crack.admission, fire.admission) == ("accepted", "accepted")
    assert camera.engine.get_observation("obs_0001") is not None
    assert camera.engine.get_observation("obs_0002") is not None


def test_each_scenario_uses_only_its_own_model_as_evidence() -> None:
    camera = Camera(engine_for(profile("two_models.yaml")))
    decisions = []
    for i in range(3):
        decisions += camera.step("crack-detector", i * 100 * MS, f"f{i}", ["crack"]).decisions
        decisions += camera.step("fire-detector", i * 100 * MS, f"f{i}", []).decisions
    by = {(d.scenario_id, d.state): d for d in decisions}
    crack_ids = {f"obs_{n:04d}" for n in (1, 3, 5)}
    fire_ids = {f"obs_{n:04d}" for n in (2, 4, 6)}
    assert set(by[(CRACK, "warning")].evidence_ids) <= crack_ids
    assert set(by[(FIRE, "clear")].evidence_ids) <= fire_ids
    assert by[(CRACK, "warning")].model_versions == {"crack-detector": "0"}
    assert by[(FIRE, "clear")].model_versions == {"fire-detector": "0"}


# --- T-R19: one model stops, only its scenario becomes unknown ---


def test_stopped_model_makes_only_its_scenario_unknown() -> None:
    camera = Camera(engine_for(profile("two_models.yaml")))
    for i in range(3):
        camera.step("crack-detector", i * 100 * MS, f"f{i}", ["crack"])
        camera.step("fire-detector", i * 100 * MS, f"f{i}", [])
    assert camera.engine.states() == {CRACK: "warning", FIRE: "clear"}

    late = []
    for i, now_ms in enumerate([400, 800, 1200, 1600, 1800], start=3):
        late += camera.step("crack-detector", now_ms * MS, f"f{i}", ["crack"]).decisions
    assert [(d.scenario_id, d.state, d.reason_codes) for d in late] == [
        (FIRE, "unknown", ["input_stale"])
    ]
    assert camera.engine.states() == {CRACK: "warning", FIRE: "unknown"}


def test_tr19_fixture_matches_hand_derived_expectation() -> None:
    from voa.replay.runner import run_events
    from voa.simulator.fixture import load_fixture

    events = load_fixture(f"{TR19}.jsonl", FixedEpochClock())
    result = run_events(events, profile("two_models.yaml"))
    assert [decision_view(d) for d in result.decisions] == load_expected(f"{TR19}.expected.jsonl")


def test_tr19_fixture_through_replay_cli(capsys: pytest.CaptureFixture[str]) -> None:
    code = replay_main(
        [
            f"{TR19}.jsonl",
            "--profile",
            str(FIXTURES / "two_models.yaml"),
            "--expected",
            f"{TR19}.expected.jsonl",
        ]
    )
    assert code == 0, capsys.readouterr().err


# --- Failure: another model cannot complete a scenario's frame ---


def test_unreferenced_model_is_accepted_but_never_evidence() -> None:
    camera = Camera(engine_for(profile("two_models.yaml")))
    results = [
        camera.step("aux-detector", i * 100 * MS, f"f{i}", ["crack", "fire"]) for i in range(5)
    ]
    assert all(r.admission == "accepted" for r in results)
    assert all(r.decisions == [] for r in results)
    assert camera.engine.states() == {CRACK: "unknown", FIRE: "unknown"}


def test_track_from_a_different_tracker_does_not_complete_a_track_frame() -> None:
    engine = engine_for(profile("valid.yaml"))
    person = Track(
        track_id="t1",
        label="person",
        bbox=(0.3, 0.4, 0.7, 0.9),
        score=0.9,
        quality="good",
        motion_class="moving",
        age_frames=3,
    )

    def frame(i: int, tracker: str) -> list[StepResult]:
        now = i * 100 * MS
        detection = build_observation(
            observation_id=f"d{i}",
            seq=i,
            capture_ts_monotonic_ns=now,
            server_received_mono_ns=now,
            payload=DetectionPayload(kind="object_detection", frame_id=f"f{i}", detections=[]),
        )
        track = build_observation(
            observation_id=f"t{i}",
            seq=i,
            capture_ts_monotonic_ns=now,
            server_received_mono_ns=now,
            model=ModelRef(id=tracker, revision="0"),
            payload=TrackPayload(kind="track_state", frame_id=f"f{i}", tracks=[person]),
        )
        return [engine.step(detection, now), engine.step(track, now)]

    rogue = frame(0, "other-tracker")
    assert all(r.admission == "accepted" for r in rogue)
    assert engine.states()[MOVING] == "unknown"
    proper = frame(1, "mock-tracker")
    assert [d.state for r in proper for d in r.decisions if d.scenario_id == MOVING] == [
        "candidate"
    ]


def test_seq_order_is_checked_per_model() -> None:
    camera = Camera(engine_for(profile("two_models.yaml")))
    for i in range(5):
        camera.step("crack-detector", i * MS, f"f{i}", [])
    assert camera.step("fire-detector", 10 * MS, "f9", []).admission == "accepted"  # fire seq 0
    replay = camera.obs("crack-detector", 11 * MS, "f10", []).model_copy(update={"seq": 4})
    assert camera.engine.step(replay, 11 * MS).admission == "out_of_order"


# --- Window capacity is per model key ---


def test_window_capacity_is_per_model() -> None:
    camera = Camera(engine_for(profile("two_models.yaml", window_max_items=2)))
    camera.step("fire-detector", 0, "f0", [])  # obs_0001
    for i in range(1, 4):
        camera.step("crack-detector", i * MS, f"f{i}", [])  # obs_0002..0004
    assert camera.engine.get_observation("obs_0002") is None  # oldest crack evicted
    assert camera.engine.get_observation("obs_0003") is not None
    assert camera.engine.get_observation("obs_0001") is not None  # fire untouched


# --- Delay: a fresh frame from one model never revives another model's stale scenario ---


def test_fresh_other_model_input_does_not_refresh_a_stale_scenario() -> None:
    camera = Camera(engine_for(profile("two_models.yaml")))
    for i in range(3):
        camera.step("fire-detector", i * 100 * MS, f"f{i}", ["fire"])
    assert camera.engine.states()[FIRE] == "warning"
    result = camera.step("crack-detector", 1701 * MS, "f9", [])
    assert [(d.scenario_id, d.state) for d in result.decisions] == [(FIRE, "unknown")]
