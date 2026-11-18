"""TASK-11 invariant suite: INV-01..10 over seeded random sequences (G1, G2).

Randomness lives only in this test (`random.Random(seed)`), never in the engine.
The generator knows the intended verdict of every frame it emits, so INV-04 is
checked against an oracle that is independent of the engine's own counters.
"""

import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.builders import build_observation
from tests.engine.test_purity import VOA_ROOT
from voa.config.profile_loader import load_profile
from voa.contracts.decision import RiskDecision
from voa.contracts.enums import HealthStatus, QualityFlag, RiskState, Verdict
from voa.contracts.observation import (
    Detection,
    DetectionPayload,
    HealthPayload,
    ModelRef,
    Observation,
    Payload,
    Track,
    TrackPayload,
)
from voa.engine.engine import DecisionEngine
from voa.engine.ports import FixedEpochClock, SequentialIdFactory
from voa.engine.profile import PolicyProfile, ProfileError

MS = 1_000_000
SEEDS = range(200)
SESSION, DEVICE = "sess_0001", "dev_0001"
MOVING = "moving_object_in_attention_zone"
OBSTACLE = "visible_obstacle_in_corridor"
MODEL = ModelRef(id="mock-detector", revision="0")
TRACKER = ModelRef(id="mock-tracker", revision="0")
AUX = ModelRef(id="aux-detector", revision="0")  # referenced by no scenario (ADR-0004)
OBSTACLE_BLOCKING: frozenset[QualityFlag] = frozenset({"occluded", "frame_dropped"})
MOVING_BLOCKING: frozenset[QualityFlag] = frozenset(
    {"occluded", "camera_motion_uncompensated", "frame_dropped"}
)


def _load() -> PolicyProfile:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "profiles" / "valid.yaml"
    result = load_profile(path)
    assert isinstance(result, PolicyProfile)
    return result


PROFILE = _load()
STALENESS_NS = PROFILE.params.staleness_ms * MS
CLEAR_FRAMES = PROFILE.params.clear_frames
SCENARIO_ORDER = [scenario.id for scenario in PROFILE.scenarios]
WARNING_TEXT = {scenario.id: scenario.warning_text for scenario in PROFILE.scenarios}


# --- Seeded action generator (pure data, so a sequence can be replayed) ---


@dataclass(frozen=True)
class Action:
    kind: str  # obstacle, person, health, tick, dup, ooo, stale, foreign, aux
    dt_ms: int
    variant: str = ""
    flags: frozenset[QualityFlag] = frozenset()
    track_flags: frozenset[QualityFlag] = frozenset()
    delay_ms: int = 0
    status: HealthStatus = "ok"
    stream: str = "camera_0"


def generate(seed: int, length: int = 90) -> list[Action]:
    rng = random.Random(seed)
    actions: list[Action] = []

    def gap() -> int:
        # Mostly steady input, sometimes a gap past unknown_after_ms or window_ms.
        return rng.randint(0, 400) if rng.random() < 0.93 else rng.randint(1400, 6000)

    while len(actions) < length:
        kind = rng.choices(
            ["obstacle", "person", "health", "tick", "dup", "ooo", "stale", "foreign", "aux"],
            weights=[30, 25, 5, 10, 3, 3, 3, 2, 6],
        )[0]
        if kind == "obstacle":
            variant = rng.choices(["met", "absent", "blocked"], weights=[5, 4, 1])[0]
            flags: frozenset[QualityFlag] = (
                frozenset({rng.choice(["occluded", "frame_dropped"])})
                if variant == "blocked"
                else frozenset()
            )
            actions += [Action(kind, gap(), variant, flags) for _ in range(rng.randint(1, 5))]
        elif kind == "person":
            variant = rng.choices(
                ["met", "absent", "static", "lost", "shaky", "occluded"],
                weights=[6, 3, 1, 1, 1, 1],
            )[0]
            for _ in range(rng.randint(1, 5)):
                delay = rng.randint(0, 300) if rng.random() < 0.9 else rng.randint(900, 1300)
                actions.append(
                    Action(
                        kind,
                        gap(),
                        variant,
                        flags=frozenset({"occluded"}) if variant == "occluded" else frozenset(),
                        track_flags=(
                            frozenset({"camera_motion_uncompensated"})
                            if variant == "shaky"
                            else frozenset()
                        ),
                        delay_ms=delay,
                    )
                )
        elif kind == "health":
            statuses: list[HealthStatus] = ["ok", "degraded", "down"]
            status = rng.choices(statuses, weights=[3, 1, 2])[0]
            stream = rng.choice(["camera_0", "camera_0", "mic_0"])
            actions.append(Action(kind, gap(), status=status, stream=stream))
        else:
            actions.append(Action(kind, gap()))
    return actions


# --- Runner: feeds the engine, records the oracle verdicts per call ---


@dataclass
class Call:
    now: int
    admission: str | None  # None for tick
    decisions: list[RiskDecision]
    verdicts: dict[str, Verdict]  # oracle verdict of frames completed in this call
    own: bool  # generated as a valid in-order observation
    camera_down: bool = False  # last accepted camera_0 health report was down


@dataclass
class Runner:
    engine: DecisionEngine
    shadow: DecisionEngine | None = None  # INV-07 twin: rejected steps replaced by tick
    now: int = 0
    count: int = 0
    seqs: dict[tuple[str, str, str], int] = field(default_factory=dict)
    last: Observation | None = None
    camera_down: bool = False
    calls: list[Call] = field(default_factory=list)

    def _new(
        self,
        payload: Payload,
        *,
        stream: str = "camera_0",
        flags: frozenset[QualityFlag] = frozenset(),
        received: int | None = None,
        session_id: str = SESSION,
        bump_seq: bool = True,
        model: ModelRef | None = None,
    ) -> Observation:
        self.count += 1
        if model is None:
            model = TRACKER if payload.kind == "track_state" else MODEL
        key = (stream, payload.kind, model.id)
        seq = self.seqs.get(key, -1) + 1
        if bump_seq:
            self.seqs[key] = seq
        return build_observation(
            observation_id=f"obs_{self.count:05d}",
            session_id=session_id,
            device_id=DEVICE,
            stream_id=stream,
            seq=seq,
            capture_ts_monotonic_ns=self.now,
            server_received_mono_ns=self.now if received is None else received,
            model=model,
            quality_flags=flags,
            payload=payload,
        )

    def _step(self, obs: Observation, verdicts: dict[str, Verdict], *, own: bool) -> str:
        result = self.engine.step(obs, self.now)
        if self.shadow is not None:
            twin = self.shadow.step(obs, self.now) if result.admission == "accepted" else None
            twin_decisions = twin.decisions if twin else self.shadow.tick(self.now)
            assert [d.model_dump_json() for d in twin_decisions] == [
                d.model_dump_json() for d in result.decisions
            ], "INV-07: a non-accepted observation changed the outcome"
        accepted = result.admission == "accepted"
        self.calls.append(
            Call(
                self.now,
                result.admission,
                result.decisions,
                verdicts if accepted else {},
                own,
                self.camera_down,
            )
        )
        if own and accepted:
            self.last = obs
        return result.admission

    def _tick(self) -> None:
        decisions = self.engine.tick(self.now)
        if self.shadow is not None:
            assert [d.model_dump_json() for d in self.shadow.tick(self.now)] == [
                d.model_dump_json() for d in decisions
            ]
        self.calls.append(Call(self.now, None, decisions, {}, True, self.camera_down))

    def run(self, actions: list[Action]) -> list[Call]:
        for action in actions:
            self.now += action.dt_ms * MS
            getattr(self, f"_do_{action.kind}")(action)
        return self.calls

    def _do_tick(self, action: Action) -> None:
        self._tick()

    def _do_obstacle(self, action: Action) -> None:
        detections = (
            [Detection(label="obstacle", bbox=(0.3, 0.5, 0.7, 0.9), score=0.9)]
            if action.variant != "absent"
            else []
        )
        payload = DetectionPayload(
            kind="object_detection", frame_id=f"of_{self.count + 1:05d}", detections=detections
        )
        verdict: Verdict = (
            "indeterminate"
            if action.flags & OBSTACLE_BLOCKING
            else ("absent" if action.variant == "absent" else "met")
        )
        self._step(self._new(payload, flags=action.flags), {OBSTACLE: verdict}, own=True)

    def _do_person(self, action: Action) -> None:
        frame_id = f"pf_{self.count + 1:05d}"
        detection = DetectionPayload(kind="object_detection", frame_id=frame_id, detections=[])
        obstacle_verdict: Verdict = (
            "indeterminate" if action.flags & OBSTACLE_BLOCKING else "absent"
        )
        detected_at = self.now
        self._step(self._new(detection, flags=action.flags), {OBSTACLE: obstacle_verdict}, own=True)

        tracks = []
        if action.variant != "absent":
            tracks.append(
                Track(
                    track_id="t1",
                    label="person",
                    bbox=(0.3, 0.4, 0.7, 0.9),
                    score=0.9,
                    quality="lost" if action.variant == "lost" else "good",
                    motion_class="static" if action.variant == "static" else "moving",
                    age_frames=3,
                )
            )
        self.now += action.delay_ms * MS
        track = TrackPayload(kind="track_state", frame_id=frame_id, tracks=tracks)
        verdicts: dict[str, Verdict] = {}
        if self.now - detected_at <= STALENESS_NS:  # otherwise the frame is discarded
            if (action.flags | action.track_flags) & MOVING_BLOCKING or action.variant == "lost":
                verdicts[MOVING] = "indeterminate"
            elif action.variant in ("absent", "static"):
                verdicts[MOVING] = "absent"
            else:
                verdicts[MOVING] = "met"
        self._step(self._new(track, flags=action.track_flags), verdicts, own=True)

    def _do_health(self, action: Action) -> None:
        payload = HealthPayload(kind="health", status=action.status, detail_code="synthetic")
        if action.stream == "camera_0":
            self.camera_down = action.status == "down"
        self._step(self._new(payload, stream=action.stream), {}, own=True)

    def _do_dup(self, action: Action) -> None:
        if self.last is None:
            return self._tick()
        self._step(self.last, {}, own=False)

    def _do_ooo(self, action: Action) -> None:
        if self.last is None:
            return self._tick()
        replay = self.last.model_copy(
            update={"observation_id": f"obs_r{self.count:05d}", "seq": self.last.seq}
        )
        self.count += 1
        self._step(replay, {}, own=False)

    def _do_stale(self, action: Action) -> None:
        payload = DetectionPayload(
            kind="object_detection",
            frame_id=f"sf_{self.count + 1:05d}",
            detections=[Detection(label="obstacle", bbox=(0.3, 0.5, 0.7, 0.9), score=0.9)],
        )
        late = self.now - STALENESS_NS - MS
        self._step(self._new(payload, received=late, bump_seq=False), {}, own=False)

    def _do_aux(self, action: Action) -> None:
        """Another model reports an obstacle on a frame the scenario model never saw (ADR-0004).

        No scenario names aux-detector, so it must be accepted (own seq) and never
        count as evidence: the oracle records no verdict for it. A fresh frame_id is
        used on purpose; a completed frame_id would be ignored even without the
        model filter and would hide the bug this action is meant to expose.
        """
        payload = DetectionPayload(
            kind="object_detection",
            frame_id=f"af_{self.count + 1:05d}",
            detections=[Detection(label="obstacle", bbox=(0.3, 0.5, 0.7, 0.9), score=0.9)],
        )
        self._step(self._new(payload, model=AUX), {}, own=True)

    def _do_foreign(self, action: Action) -> None:
        payload = DetectionPayload(
            kind="object_detection",
            frame_id=f"ff_{self.count + 1:05d}",
            detections=[Detection(label="obstacle", bbox=(0.3, 0.5, 0.7, 0.9), score=0.9)],
        )
        self._step(self._new(payload, session_id="sess_other", bump_seq=False), {}, own=False)


def make_engine(profile: PolicyProfile | ProfileError = PROFILE) -> DecisionEngine:
    return DecisionEngine(SESSION, DEVICE, profile, SequentialIdFactory(), FixedEpochClock())


def run_seed(
    seed: int, *, check: Callable[[DecisionEngine, Call], None] | None = None
) -> list[Call]:
    runner = Runner(make_engine(), shadow=make_engine())
    calls: list[Call] = []
    for action in generate(seed):
        start = len(runner.calls)
        runner.run([action])
        for call in runner.calls[start:]:
            if check is not None:
                check(runner.engine, call)  # evaluated at decision time, before the next call
        calls = runner.calls
    return calls


# --- Per-decision invariant checker ---


class Checker:
    def __init__(self) -> None:
        self.last_state: dict[str, RiskState] = dict.fromkeys(SCENARIO_ORDER, "unknown")
        self.absent_run: Counter[str] = Counter()
        self.alert_ids: set[str] = set()
        self.transitions: Counter[tuple[str, str, str]] = Counter()
        self.admissions: Counter[str] = Counter()

    def __call__(self, engine: DecisionEngine, call: Call) -> None:
        if call.admission is not None:
            self.admissions[call.admission] += 1
            if call.own:
                assert call.admission == "accepted", call
            else:
                assert call.admission != "accepted", call
        for scenario_id, verdict in call.verdicts.items():
            self.absent_run[scenario_id] = (
                self.absent_run[scenario_id] + 1 if verdict == "absent" else 0
            )

        order = [SCENARIO_ORDER.index(d.scenario_id) for d in call.decisions]
        assert order == sorted(set(order)), "one transition per scenario, in profile order"
        for d in call.decisions:
            self.check_decision(engine, call, d)

        # G2 / rows 1, 9, 10: while a required sensor is down nothing leaves unknown.
        if call.camera_down:
            assert set(engine.states().values()) == {"unknown"}
            assert all(d.state == "unknown" for d in call.decisions)

    def check_decision(self, engine: DecisionEngine, call: Call, d: RiskDecision) -> None:
        scenario = d.scenario_id
        reasons = tuple(d.reason_codes)
        self.transitions[(d.previous_state or "", d.state, reasons[0])] += 1
        assert (d.session_id, d.device_id) == (SESSION, DEVICE)
        assert d.policy_version == "indoor_research_v0@1"
        assert d.decided_at_utc == FixedEpochClock().at(call.now)

        # INV-09: decisions only on change, chained from the previous decision.
        assert d.previous_state == self.last_state[scenario]
        assert d.state != d.previous_state
        self.last_state[scenario] = d.state

        # INV-03
        assert not (d.previous_state == "unknown" and d.state == "warning")

        if call.admission is None:
            assert d.evidence_ids == [], "tick decisions carry no evidence"

        if d.state == "warning":
            assert call.verdicts.get(scenario) == "met"
            # INV-01: evidence exists, is indexed and belongs to this session.
            assert d.evidence_ids
            for evidence_id in d.evidence_ids:
                observation = engine.get_observation(evidence_id)
                assert observation is not None
                assert (observation.session_id, observation.device_id) == (SESSION, DEVICE)
                # INV-02: every evidence item is fresh at decision time.
                assert call.now - observation.server_received_mono_ns <= STALENESS_NS
                assert engine.is_fresh(evidence_id, call.now)
            # INV-08: approved template text only; ENG-10: a new alert per entry.
            assert d.alert is not None
            assert d.alert.text == WARNING_TEXT[scenario]
            assert d.alert.text_kind == "approved_template"
            assert d.alert.alert_id not in self.alert_ids
            self.alert_ids.add(d.alert.alert_id)
        else:
            assert d.alert is None

        # INV-04 (G2): clearing needs clear_frames consecutive fresh absent frames.
        clears = d.state == "resolved" or (
            d.state == "clear" and d.previous_state in ("candidate", "unknown")
        )
        if clears:
            assert call.verdicts.get(scenario) == "absent"
            assert self.absent_run[scenario] >= CLEAR_FRAMES
            assert reasons == ("condition_absent_confirmed",)
        if d.state == "clear" and d.previous_state == "resolved":
            assert reasons == ("episode_closed",)

        # G2: loss of input or sensor never reads as safe.
        if "input_stale" in reasons or "sensor_down" in reasons:
            assert d.state == "unknown"


# --- Tests ---


@pytest.mark.parametrize("seed", SEEDS)
def test_invariants_hold_for_every_decision(seed: int) -> None:
    """INV-01/02/03/04/07/08/09 checked at every decision of a random sequence."""
    run_seed(seed, check=Checker())


def test_generator_exercises_every_path() -> None:
    """Guard against a vacuous suite: the 200 sequences must reach every row."""
    checker = Checker()
    totals: Counter[tuple[str, str, str]] = Counter()
    admissions: Counter[str] = Counter()
    for seed in SEEDS:
        checker = Checker()
        run_seed(seed, check=checker)
        totals.update(checker.transitions)
        admissions.update(checker.admissions)

    expected = {
        ("unknown", "candidate", "fresh_evidence_returned"),
        ("unknown", "clear", "condition_absent_confirmed"),
        ("clear", "candidate", "condition_observed"),
        ("candidate", "warning", "temporal_confirmation_passed"),
        ("candidate", "clear", "condition_absent_confirmed"),
        ("warning", "resolved", "condition_absent_confirmed"),
        ("resolved", "clear", "episode_closed"),
        ("resolved", "candidate", "condition_observed"),
        ("warning", "unknown", "input_stale"),
        ("warning", "unknown", "sensor_down"),
        ("candidate", "unknown", "input_stale"),
        ("clear", "unknown", "sensor_down"),
    }
    assert expected <= set(totals), sorted(expected - set(totals))
    for status in (
        "accepted",
        "duplicate",
        "out_of_order",
        "stale_on_arrival",
        "rejected_session_mismatch",
    ):
        assert admissions[status] > 0, status


@pytest.mark.parametrize("seed", SEEDS[:50])
def test_inv05_same_inputs_give_byte_identical_decisions(seed: int) -> None:
    def decisions() -> list[str]:
        return [d.model_dump_json() for c in run_seed(seed) for d in c.decisions]

    first, second = decisions(), decisions()
    assert first == second


def test_inv06_purity_scan_covers_decision_engine() -> None:
    assert (VOA_ROOT / "engine" / "engine.py").is_file()
    source = (VOA_ROOT / "engine" / "engine.py").read_text(encoding="utf-8")
    from tests.engine.test_purity import assert_pure_imports

    assert_pure_imports(source, "voa.engine.engine")


@pytest.mark.parametrize("seed", SEEDS[:50])
def test_inv10_invalid_profile_decides_nothing(seed: int) -> None:
    error = ProfileError(code="profile_invalid", detail="profile.yaml: params")
    runner = Runner(make_engine(error))
    calls = runner.run(generate(seed))
    assert all(c.admission in (None, "config_error") for c in calls)
    assert all(c.decisions == [] for c in calls)
    assert runner.engine.states() == {}
