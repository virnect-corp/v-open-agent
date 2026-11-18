"""TASK-10 DecisionEngine integration, stage 6 decisions (SPEC 4.1, 4.7)."""

from datetime import timedelta
from pathlib import Path

import pytest

from tests.builders import build_observation
from voa.config.profile_loader import load_profile
from voa.contracts.enums import HealthStatus, QualityFlag
from voa.contracts.observation import (
    Detection,
    DetectionPayload,
    HealthPayload,
    ModelRef,
    Observation,
    Track,
    TrackPayload,
)
from voa.engine.engine import DecisionEngine, StepResult
from voa.engine.ports import FixedEpochClock, SequentialIdFactory
from voa.engine.profile import PolicyProfile, ProfileError

MS = 1_000_000
MOVING = "moving_object_in_attention_zone"
OBSTACLE = "visible_obstacle_in_corridor"
OBSTACLE_TEXT = "전방 통로에 장애물이 보입니다."
MODEL = ModelRef(id="mock-detector", revision="0")
TRACKER = ModelRef(id="mock-tracker", revision="0")


@pytest.fixture
def profile() -> PolicyProfile:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "profiles" / "valid.yaml"
    result = load_profile(path)
    assert isinstance(result, PolicyProfile)
    return result


def make_engine(profile: PolicyProfile | ProfileError) -> DecisionEngine:
    return DecisionEngine(
        "sess_0001", "dev_0001", profile, SequentialIdFactory(), FixedEpochClock()
    )


class Feed:
    """Build in-order camera_0 observations received exactly at `now`."""

    def __init__(self, engine: DecisionEngine) -> None:
        self.engine = engine
        self.count = 0

    def _obs(self, now: int, payload: object, **overrides: object) -> Observation:
        self.count += 1
        fields: dict[str, object] = {
            "observation_id": f"obs_{self.count:04d}",
            "seq": self.count,
            "capture_ts_monotonic_ns": now,
            "server_received_mono_ns": now,
            "model": TRACKER if getattr(payload, "kind", "") == "track_state" else MODEL,
            "payload": payload,
        }
        fields.update(overrides)
        return build_observation(**fields)  # type: ignore[arg-type]

    def obstacle(
        self, now: int, *, present: bool = True, flags: frozenset[QualityFlag] = frozenset()
    ) -> StepResult:
        detections = (
            [Detection(label="obstacle", bbox=(0.3, 0.5, 0.7, 0.9), score=0.9)] if present else []
        )
        payload = DetectionPayload(
            kind="object_detection", frame_id=f"frame_{self.count + 1:04d}", detections=detections
        )
        return self.engine.step(self._obs(now, payload, quality_flags=flags), now)

    def person_frame(self, now: int, *, track_delay_ns: int = 0) -> list[StepResult]:
        """Detection then moving person track; the detection also reads as obstacle-absent."""
        frame_id = f"pframe_{self.count + 1:04d}"
        detection = DetectionPayload(kind="object_detection", frame_id=frame_id, detections=[])
        track = TrackPayload(
            kind="track_state",
            frame_id=frame_id,
            tracks=[
                Track(
                    track_id="t1",
                    label="person",
                    bbox=(0.3, 0.4, 0.7, 0.9),
                    score=0.9,
                    quality="good",
                    motion_class="moving",
                    age_frames=3,
                )
            ],
        )
        first = self.engine.step(self._obs(now, detection), now)
        later = now + track_delay_ns
        return [first, self.engine.step(self._obs(later, track), later)]

    def health(self, now: int, status: HealthStatus) -> StepResult:
        payload = HealthPayload(kind="health", status=status, detail_code="test")
        return self.engine.step(self._obs(now, payload), now)


def states_of(results: list[StepResult]) -> list[tuple[str, str]]:
    return [(d.scenario_id, d.state) for r in results for d in r.decisions]


def to_warning(feed: Feed, start: int = 0) -> int:
    """Three met obstacle frames 100 ms apart; return the time of the last one."""
    for i in range(3):
        feed.obstacle(start + i * 100 * MS)
    return start + 200 * MS


# --- Success: confirmation and decision fields (T-R01, ENG-06, ENG-11, ENG-13, INV-08) ---


def test_confirm_frames_produce_candidate_then_one_warning_with_alert(
    profile: PolicyProfile,
) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    results = [feed.obstacle(i * 100 * MS) for i in range(3)]

    assert [r.admission for r in results] == ["accepted"] * 3
    assert [len(r.decisions) for r in results] == [1, 0, 1]
    candidate = results[0].decisions[0]
    assert (candidate.previous_state, candidate.state) == ("unknown", "candidate")
    assert candidate.reason_codes == ["fresh_evidence_returned"]
    assert candidate.risk_event_id == "risk_0001"
    assert candidate.severity is None and candidate.alert is None

    warning = results[2].decisions[0]
    clock = FixedEpochClock()
    assert warning.scenario_id == OBSTACLE
    assert (warning.previous_state, warning.state) == ("candidate", "warning")
    assert warning.reason_codes == ["temporal_confirmation_passed"]
    assert warning.risk_event_id == "risk_0001"
    assert warning.session_id == "sess_0001" and warning.device_id == "dev_0001"
    assert warning.policy_version == "indoor_research_v0@1"
    assert warning.severity == "attention"
    assert warning.evidence_ids == ["obs_0003"]
    assert warning.model_versions == {"mock-detector": "0"}
    assert warning.decided_at_utc == clock.at(200 * MS)
    assert warning.fixture_note is None
    assert warning.alert is not None
    assert warning.alert.alert_id == "alert_0001"
    assert warning.alert.text == OBSTACLE_TEXT
    assert warning.alert.text_kind == "approved_template"
    assert warning.alert.delivery_state == "queued"
    assert warning.alert.valid_until_utc == clock.at(200 * MS) + timedelta(milliseconds=4000)
    assert engine.states() == {MOVING: "unknown", OBSTACLE: "warning"}


def test_warning_evidence_is_indexed_fresh_and_same_session(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    now = to_warning(feed)
    warning = engine.states()
    assert warning[OBSTACLE] == "warning"
    for evidence_id in ["obs_0003"]:
        observation = engine.get_observation(evidence_id)
        assert observation is not None and observation.session_id == "sess_0001"
        assert engine.is_fresh(evidence_id, now)


def test_continued_met_in_warning_emits_nothing_and_no_second_alert(
    profile: PolicyProfile,
) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    now = to_warning(feed)
    later = [feed.obstacle(now + i * 100 * MS) for i in range(1, 6)]
    assert all(r.decisions == [] for r in later)


def test_start_unknown_to_clear_uses_none_event_id(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    results = [feed.obstacle(i * 100 * MS, present=False) for i in range(3)]
    assert [len(r.decisions) for r in results] == [0, 0, 1]
    clear = results[2].decisions[0]
    assert (clear.previous_state, clear.state) == ("unknown", "clear")
    assert clear.reason_codes == ["condition_absent_confirmed"]
    assert clear.risk_event_id == "none"
    assert clear.evidence_ids == ["obs_0003"]


# --- Time rules: tick and non-completing steps (rows 1/2/4, T-R03) ---


def test_tick_after_input_gap_goes_unknown_not_resolved(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    now = to_warning(feed)

    assert engine.tick(now + 1500 * MS) == []  # boundary is exclusive
    decisions = engine.tick(now + 1501 * MS)
    assert [(d.previous_state, d.state) for d in decisions] == [("warning", "unknown")]
    unknown = decisions[0]
    assert unknown.reason_codes == ["input_stale"]
    assert unknown.risk_event_id == "risk_0001"
    assert unknown.evidence_ids == [] and unknown.model_versions == {}
    assert unknown.decided_at_utc == FixedEpochClock().at(now + 1501 * MS)
    assert engine.tick(now + 3000 * MS) == []  # INV-09: no repeated state


def test_reentry_after_unknown_passes_candidate_and_creates_new_alert(
    profile: PolicyProfile,
) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    now = to_warning(feed)
    restart = now + 2000 * MS
    assert engine.tick(restart)[0].state == "unknown"

    results = [feed.obstacle(restart + i * 100 * MS) for i in range(3)]
    decisions = [d for r in results for d in r.decisions]
    assert [(d.previous_state, d.state) for d in decisions] == [
        ("unknown", "candidate"),
        ("candidate", "warning"),
    ]
    assert {d.risk_event_id for d in decisions} == {"risk_0001"}
    assert decisions[1].alert is not None
    assert decisions[1].alert.alert_id == "alert_0002"


def test_tick_does_not_touch_counters(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    feed.obstacle(0)
    feed.obstacle(100 * MS)
    assert engine.tick(200 * MS) == []
    assert engine.tick(300 * MS) == []
    assert feed.obstacle(400 * MS).decisions[0].state == "warning"


def test_resolved_leaves_on_next_tick_with_closing_event_id(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    now = to_warning(feed)
    absent = [feed.obstacle(now + i * 100 * MS, present=False) for i in range(1, 4)]
    assert states_of(absent) == [(OBSTACLE, "resolved")]

    decisions = engine.tick(now + 400 * MS)
    assert [(d.previous_state, d.state) for d in decisions] == [("resolved", "clear")]
    assert decisions[0].reason_codes == ["episode_closed"]
    assert decisions[0].risk_event_id == "risk_0001"
    assert decisions[0].evidence_ids == []

    restarted = feed.obstacle(now + 500 * MS).decisions[0]
    assert (restarted.state, restarted.risk_event_id) == ("candidate", "risk_0002")


def test_non_completing_step_applies_only_time_rules(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    feed.obstacle(0)
    result = feed.health(1501 * MS, "ok")
    assert result.admission == "accepted"
    assert [(d.state, d.reason_codes, d.evidence_ids) for d in result.decisions] == [
        ("unknown", ["input_stale"], [])
    ]


def test_health_down_moves_scenarios_to_unknown_in_profile_order(
    profile: PolicyProfile,
) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    feed.obstacle(0)
    feed.person_frame(100 * MS)
    assert engine.states() == {MOVING: "candidate", OBSTACLE: "candidate"}

    result = feed.health(200 * MS, "down")
    assert [(d.scenario_id, d.state, d.reason_codes) for d in result.decisions] == [
        (MOVING, "unknown", ["sensor_down"]),
        (OBSTACLE, "unknown", ["sensor_down"]),
    ]
    # Met frames while the sensor is down cannot leave unknown (row 9 needs no down stream).
    assert feed.obstacle(300 * MS).decisions == []
    feed.health(400 * MS, "ok")
    assert feed.obstacle(500 * MS).decisions[0].state == "candidate"


def test_blocked_frames_never_warn_and_go_unknown_with_reason(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    feed.obstacle(0)
    blocked = [
        feed.obstacle(i * 100 * MS, flags=frozenset({"occluded"})) for i in range(1, 17)
    ]  # last determinate at 0; 1600 ms exceeds unknown_after_ms=1500
    assert all(d.state != "warning" for r in blocked for d in r.decisions)
    decisions = [d for r in blocked for d in r.decisions]
    assert [(d.state, d.reason_codes) for d in decisions] == [
        ("unknown", ["input_stale", "quality_blocked"])
    ]


# --- Failure / missing / delay: admission and freshness (INV-07, ENG-02, ENG-03) ---


def test_rejected_observations_do_not_count_toward_confirmation(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    feed.obstacle(0)
    feed.obstacle(100 * MS)
    payload = DetectionPayload(
        kind="object_detection",
        frame_id="frame_x",
        detections=[Detection(label="obstacle", bbox=(0.3, 0.5, 0.7, 0.9), score=0.9)],
    )
    rejected = [
        build_observation(  # duplicate id
            observation_id="obs_0002",
            seq=99,
            capture_ts_monotonic_ns=150 * MS,
            server_received_mono_ns=150 * MS,
            payload=payload,
        ),
        build_observation(  # old seq
            observation_id="obs_r1",
            seq=1,
            capture_ts_monotonic_ns=150 * MS,
            server_received_mono_ns=150 * MS,
            payload=payload,
        ),
        build_observation(  # another session
            observation_id="obs_r2",
            session_id="sess_other",
            seq=50,
            capture_ts_monotonic_ns=150 * MS,
            server_received_mono_ns=150 * MS,
            payload=payload,
        ),
        build_observation(  # arrived already stale
            observation_id="obs_r3",
            seq=60,
            capture_ts_monotonic_ns=150 * MS,
            server_received_mono_ns=150 * MS - 1001 * MS,
            payload=payload,
        ),
    ]
    statuses = [engine.step(obs, 150 * MS) for obs in rejected]
    assert [s.admission for s in statuses] == [
        "duplicate",
        "out_of_order",
        "rejected_session_mismatch",
        "stale_on_arrival",
    ]
    assert all(s.decisions == [] for s in statuses)
    assert engine.states()[OBSTACLE] == "candidate"
    assert engine.get_observation("obs_r1") is None
    assert feed.obstacle(200 * MS).decisions[0].state == "warning"


def test_frame_completed_after_staleness_is_not_evaluated(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    results = feed.person_frame(0, track_delay_ns=1001 * MS)
    assert all(r.admission == "accepted" for r in results)
    assert engine.states()[MOVING] == "unknown"
    assert all(d.scenario_id != MOVING for r in results for d in r.decisions)


# --- Clock and read-only queries (SPEC 4.1) ---


def test_time_going_backwards_raises_value_error(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    feed.obstacle(500 * MS)
    assert engine.tick(500 * MS) == []  # equal time is allowed
    with pytest.raises(ValueError):
        engine.tick(499 * MS)
    with pytest.raises(ValueError):
        feed.obstacle(400 * MS)
    with pytest.raises(ValueError):
        engine.is_fresh("obs_0001", 499 * MS)


def test_queries_are_read_only_and_do_not_advance_time(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    feed.obstacle(0)
    assert engine.is_fresh("obs_0001", 1000 * MS)
    assert not engine.is_fresh("obs_0001", 1001 * MS)
    assert not engine.is_fresh("obs_unknown", 1001 * MS)
    assert engine.get_observation("obs_unknown") is None
    # A far-future query neither evicts evidence nor moves the engine clock.
    assert not engine.is_fresh("obs_0001", 10_000 * MS)
    assert engine.get_observation("obs_0001") is not None
    assert engine.tick(50 * MS) == []
    assert engine.states() == {MOVING: "unknown", OBSTACLE: "candidate"}


def test_evicted_evidence_is_gone_from_queries(profile: PolicyProfile) -> None:
    engine = make_engine(profile)
    feed = Feed(engine)
    feed.obstacle(0)
    engine.tick(5001 * MS)
    assert engine.get_observation("obs_0001") is None
    assert engine.is_fresh("obs_0001", 5001 * MS) is False


# --- Invalid profile (INV-10, ENG-15) ---


def test_profile_error_rejects_everything_and_decides_nothing() -> None:
    engine = make_engine(ProfileError(code="profile_invalid", detail="valid.yaml: params"))
    feed = Feed(engine)
    results = [feed.obstacle(i * 100 * MS) for i in range(5)]
    assert [r.admission for r in results] == ["config_error"] * 5
    assert all(r.decisions == [] for r in results)
    assert engine.tick(10_000 * MS) == []
    assert engine.states() == {}
    assert engine.get_observation("obs_0001") is None
    assert engine.is_fresh("obs_0001", 10_000 * MS) is False


# --- Determinism (INV-05 smoke; full suite is TASK-11) ---


def test_same_inputs_produce_byte_identical_decisions(profile: PolicyProfile) -> None:
    def run() -> list[str]:
        engine = make_engine(profile)
        feed = Feed(engine)
        out = []
        now = to_warning(feed)
        feed.person_frame(now + 50 * MS)
        out += engine.tick(now + 3000 * MS)
        for i in range(3):
            out += feed.obstacle(now + 3100 * MS + i * 100 * MS, present=False).decisions
        return [d.model_dump_json() for d in out]

    first, second = run(), run()
    assert first == second and len(first) >= 3
