"""Session decision engine: stages 1-6 wired together (SPEC 4.1, 4.7)."""

from dataclasses import dataclass
from datetime import timedelta

from voa.contracts.decision import SCHEMA_VERSION, Alert, RiskDecision
from voa.contracts.enums import RiskState
from voa.contracts.observation import Observation
from voa.engine.admission import Admission, AdmissionStatus
from voa.engine.evaluator import ConditionResult, ScenarioEvaluator
from voa.engine.ports import IdFactory, UtcClock
from voa.engine.profile import PolicyProfile, ProfileError, ScenarioProfile
from voa.engine.state_machine import ScenarioStateMachine, StateTransition
from voa.engine.window import FrameAssembler, ObservationWindow


@dataclass(frozen=True)
class StepResult:
    admission: AdmissionStatus
    decisions: list[RiskDecision]


@dataclass(frozen=True)
class _ScenarioRuntime:
    scenario: ScenarioProfile
    assembler: FrameAssembler
    evaluator: ScenarioEvaluator
    machine: ScenarioStateMachine


class DecisionEngine:
    """One engine per session. Time and IDs are injected; no I/O, no wall clock.

    An invalid profile rejects every observation as config_error and never
    decides (INV-10). Non-accepted observations never reach the window,
    assemblers or counters (INV-07); the call still applies time rules 1/2/4.
    """

    def __init__(
        self,
        session_id: str,
        device_id: str,
        profile: PolicyProfile | ProfileError,
        ids: IdFactory,
        clock: UtcClock,
    ) -> None:
        self._session_id = session_id
        self._device_id = device_id
        self._profile = profile
        self._ids = ids
        self._clock = clock
        self._last_now: int | None = None
        self._admission = Admission(session_id, device_id, profile)
        self._window: ObservationWindow | None = None
        self._runtimes: tuple[_ScenarioRuntime, ...] = ()
        if isinstance(profile, PolicyProfile):
            window = ObservationWindow(profile.params)
            self._window = window
            self._runtimes = tuple(
                _ScenarioRuntime(
                    scenario,
                    FrameAssembler(scenario, window),
                    ScenarioEvaluator(scenario, profile.params),
                    ScenarioStateMachine(scenario, profile.params, ids),
                )
                for scenario in profile.scenarios
            )

    def step(self, obs: Observation, now_mono_ns: int) -> StepResult:
        """Process one observation; completed frames get the full table."""
        self._advance_time(now_mono_ns)
        window = self._window
        if window is None:
            return StepResult("config_error", [])

        self._forget(window.prune(now_mono_ns=now_mono_ns))
        status = self._admission.admit(obs, now_mono_ns=now_mono_ns)
        conditions: dict[str, ConditionResult] = {}
        if status == "accepted":
            self._forget(window.append(obs, now_mono_ns=now_mono_ns))
            for runtime in self._runtimes:
                frame = runtime.assembler.add(obs, now_mono_ns=now_mono_ns)
                if frame is not None:
                    conditions[runtime.scenario.id] = runtime.evaluator.evaluate(frame)
        else:
            self._prune_assemblers(now_mono_ns)
        return StepResult(status, self._apply(now_mono_ns, conditions))

    def tick(self, now_mono_ns: int) -> list[RiskDecision]:
        """Apply time rules 1/2/4 to every scenario; counters are untouched."""
        self._advance_time(now_mono_ns)
        window = self._window
        if window is None:
            return []
        self._forget(window.prune(now_mono_ns=now_mono_ns))
        self._prune_assemblers(now_mono_ns)
        return self._apply(now_mono_ns, {})

    def states(self) -> dict[str, RiskState]:
        return {runtime.scenario.id: runtime.machine.state for runtime in self._runtimes}

    def get_observation(self, observation_id: str) -> Observation | None:
        if self._window is None:
            return None
        return self._window.get_observation(observation_id)

    def is_fresh(self, observation_id: str, now_mono_ns: int) -> bool:
        """Read-only: rejects past times but never advances the engine clock."""
        self._check_time(now_mono_ns)
        if self._window is None:
            return False
        return self._window.is_fresh(observation_id, now_mono_ns=now_mono_ns)

    def _check_time(self, now_mono_ns: int) -> None:
        if self._last_now is not None and now_mono_ns < self._last_now:
            raise ValueError("now_mono_ns must be non-decreasing")

    def _advance_time(self, now_mono_ns: int) -> None:
        self._check_time(now_mono_ns)
        self._last_now = now_mono_ns

    def _forget(self, evicted: tuple[str, ...]) -> None:
        for observation_id in evicted:
            self._admission.forget_observation(observation_id)

    def _prune_assemblers(self, now_mono_ns: int) -> None:
        for runtime in self._runtimes:
            runtime.assembler.prune(now_mono_ns=now_mono_ns)

    def _apply(
        self, now_mono_ns: int, conditions: dict[str, ConditionResult]
    ) -> list[RiskDecision]:
        window = self._window
        assert window is not None
        decisions: list[RiskDecision] = []
        for runtime in self._runtimes:
            condition = conditions.get(runtime.scenario.id)
            sensor_down = {
                stream: window.is_sensor_down(stream)
                for stream in runtime.scenario.required_streams
            }
            transition = runtime.machine.advance(
                now_mono_ns=now_mono_ns, condition=condition, sensor_down=sensor_down
            )
            if transition is not None:
                decisions.append(
                    self._decision(runtime.scenario, transition, condition, now_mono_ns)
                )
        return decisions

    def _decision(
        self,
        scenario: ScenarioProfile,
        transition: StateTransition,
        condition: ConditionResult | None,
        now_mono_ns: int,
    ) -> RiskDecision:
        profile = self._profile
        assert isinstance(profile, PolicyProfile)
        window = self._window
        assert window is not None
        evidence_ids = list(condition.evidence_ids) if condition is not None else []
        model_versions: dict[str, str] = {}
        for observation_id in evidence_ids:
            observation = window.get_observation(observation_id)
            if observation is not None and observation.model is not None:
                model_versions[observation.model.id] = observation.model.revision

        decided_at = self._clock.at(now_mono_ns)
        alert: Alert | None = None
        if transition.state == "warning":
            alert = Alert(
                alert_id=self._ids.new("alert"),
                text=scenario.warning_text,
                text_kind="approved_template",
                delivery_state="queued",
                valid_until_utc=decided_at + timedelta(milliseconds=profile.params.warning_ttl_ms),
            )
        return RiskDecision(
            schema_version=SCHEMA_VERSION,
            risk_event_id=transition.risk_event_id or "none",
            session_id=self._session_id,
            device_id=self._device_id,
            scenario_id=scenario.id,
            policy_version=f"{profile.profile_id}@{profile.profile_version}",
            state=transition.state,
            previous_state=transition.previous_state,
            severity=scenario.severity if transition.state == "warning" else None,
            evidence_ids=evidence_ids,
            reason_codes=list(transition.reason_codes),
            model_versions=model_versions,
            decided_at_utc=decided_at,
            alert=alert,
        )
