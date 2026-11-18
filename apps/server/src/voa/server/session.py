"""One DecisionEngine per session and the messages it produces (SPEC 7.1, 7.2, 7.4).

All methods here are synchronous: the caller reads `now` and calls the engine
without an await in between, so ticks and observations stay time-ordered.
"""

from dataclasses import dataclass, field
from datetime import datetime

from voa.contracts.decision import RiskDecision
from voa.contracts.messages import AdmissionMessage, AlertMessage, DecisionMessage
from voa.contracts.observation import HealthPayload, Observation, ObservationDraft
from voa.engine.engine import DecisionEngine
from voa.engine.ports import IdFactory, UtcClock
from voa.engine.profile import PolicyProfile
from voa.storage.sqlite import SQLiteStore

PushMessage = DecisionMessage | AlertMessage | AdmissionMessage


@dataclass
class SessionStream:
    session_id: str
    device_id: str
    inputs: frozenset[str]
    engine: DecisionEngine
    ids: IdFactory
    clock: UtcClock
    store: SQLiteStore | None
    started_at_utc: datetime
    injected_ids: tuple[str, ...] = ()
    alert_seq: int = 0
    last_alert_seq: int | None = None  # from `resume`; retransmission is TASK-20
    connected: bool = field(default=False)

    @classmethod
    def start(
        cls,
        *,
        session_id: str,
        device_id: str,
        inputs: frozenset[str],
        profile: PolicyProfile,
        ids: IdFactory,
        clock: UtcClock,
        store: SQLiteStore | None,
        now_mono_ns: int,
    ) -> "SessionStream":
        stream = cls(
            session_id=session_id,
            device_id=device_id,
            inputs=inputs,
            engine=DecisionEngine(session_id, device_id, profile, ids, clock),
            ids=ids,
            clock=clock,
            store=store,
            started_at_utc=clock.at(now_mono_ns),
        )
        stream._inject_undeclared(profile, now_mono_ns)
        return stream

    def _inject_undeclared(self, profile: PolicyProfile, now_mono_ns: int) -> None:
        """SPEC 7.1: required streams the device did not declare stay `down` (T-R17)."""
        required = sorted({s for scenario in profile.scenarios for s in scenario.required_streams})
        injected = []
        for stream_id in required:
            if stream_id in self.inputs:
                continue
            draft = ObservationDraft(
                stream_id=stream_id,
                seq=0,
                capture_ts_monotonic_ns=now_mono_ns,
                payload=HealthPayload(kind="health", status="down", detail_code="not_declared"),
            )
            observation = self._observation(draft, now_mono_ns)
            self.engine.step(observation, now_mono_ns)
            injected.append(observation.observation_id)
        self.injected_ids = tuple(injected)

    def accepts_stream(self, stream_id: str) -> bool:
        return stream_id in self.inputs

    def ingest(self, draft: ObservationDraft, now_mono_ns: int) -> list[PushMessage]:
        observation = self._observation(draft, now_mono_ns)
        result = self.engine.step(observation, now_mono_ns)
        if self.store is not None:
            self.store.save_observation(observation, admission_status=result.admission)
        messages: list[PushMessage] = []
        if result.admission != "accepted":
            messages.append(
                AdmissionMessage(
                    seq=draft.seq,
                    stream_id=draft.stream_id,
                    status=result.admission,
                )
            )
        messages += self._decisions(result.decisions)
        return messages

    def tick(self, now_mono_ns: int) -> list[PushMessage]:
        return self._decisions(self.engine.tick(now_mono_ns))

    def _observation(self, draft: ObservationDraft, now_mono_ns: int) -> Observation:
        return Observation.from_draft(
            draft,
            observation_id=self.ids.new("obs"),
            session_id=self.session_id,
            device_id=self.device_id,
            server_received_mono_ns=now_mono_ns,
            received_at_utc=self.clock.at(now_mono_ns),
        )

    def _decisions(self, decisions: list[RiskDecision]) -> list[PushMessage]:
        messages: list[PushMessage] = []
        for decision in decisions:
            if self.store is not None:
                self.store.save_decision(decision)
            messages.append(DecisionMessage(decision=decision))
            if decision.alert is not None:
                self.alert_seq += 1
                messages.append(
                    AlertMessage(
                        alert_seq=self.alert_seq,
                        alert=decision.alert,
                        risk_event_id=decision.risk_event_id,
                        scenario_id=decision.scenario_id,
                    )
                )
        return messages
