"""One DecisionEngine per session and the messages it produces (SPEC 7.1, 7.2, 7.4).

All methods here are synchronous: the caller reads `now` and calls the engine
without an await in between, so ticks and observations stay time-ordered.
"""

from dataclasses import dataclass, field
from datetime import datetime

from voa.contracts.decision import RiskDecision
from voa.contracts.messages import (
    AdmissionMessage,
    AlertMessage,
    DecisionMessage,
    NoticeMessage,
)
from voa.contracts.observation import HealthPayload, Observation, ObservationDraft
from voa.engine.engine import DecisionEngine
from voa.engine.ports import IdFactory, UtcClock
from voa.engine.profile import PolicyProfile
from voa.server.backpressure import InputQueue
from voa.server.dispatch import AckOutcome, AckStatus, AlertDispatcher, AlertRecord, NoticeTracker
from voa.storage.sqlite import SQLiteStore


def _utc_text(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


PushMessage = DecisionMessage | AlertMessage | AdmissionMessage | NoticeMessage


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
    warning_ttl_ms: int
    degraded_notice_text: str
    input: InputQueue
    last_received_mono_ns: dict[str, int] = field(default_factory=dict)
    dispatcher: AlertDispatcher = field(default_factory=AlertDispatcher)
    notices: NoticeTracker = field(default_factory=NoticeTracker)
    injected_ids: tuple[str, ...] = ()
    alert_seq: int = 0
    last_alert_seq: int | None = None  # last value reported by `alert_sync`
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
        input_queue_max: int = 64,
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
            warning_ttl_ms=profile.params.warning_ttl_ms,
            degraded_notice_text=profile.degraded_notice_text,
            input=InputQueue(input_queue_max),
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

    def record_receipt(self, stream_id: str, now_mono_ns: int) -> None:
        self.last_received_mono_ns[stream_id] = now_mono_ns

    def ingest(
        self,
        draft: ObservationDraft,
        now_mono_ns: int,
        *,
        received_mono_ns: int | None = None,
    ) -> list[PushMessage]:
        """`received_mono_ns` is the queue entry time, so queueing delay counts as age."""
        received = now_mono_ns if received_mono_ns is None else received_mono_ns
        observation = self._observation(draft, received)
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
        messages += self._decisions(result.decisions, now_mono_ns)
        return messages

    def tick(self, now_mono_ns: int) -> list[PushMessage]:
        messages = self._decisions(self.engine.tick(now_mono_ns), now_mono_ns)
        for record in self.dispatcher.expire(now_mono_ns):
            self._persist_alert(record, now_mono_ns)
        return messages

    def alert_sync(self, last_alert_seq: int, now_mono_ns: int) -> list[AlertMessage]:
        """SPEC 7.3: re-send, with the same alert_id and alert_seq, only alerts after
        `last_alert_seq` whose scenario is still `warning`, that are the latest alert of
        that scenario (the current episode's last alert), and are still valid.
        """
        self.last_alert_seq = last_alert_seq
        states = self.engine.states()
        latest: dict[str, int] = {}
        for record in self.dispatcher.records.values():
            latest[record.scenario_id] = max(latest.get(record.scenario_id, 0), record.alert_seq)
        resend = [
            record
            for record in self.dispatcher.records.values()
            if record.alert_seq > last_alert_seq
            and states.get(record.scenario_id) == "warning"
            and record.alert_seq == latest[record.scenario_id]
            and now_mono_ns < record.valid_until_mono_ns
        ]
        return [
            AlertMessage(
                alert_seq=record.alert_seq,
                alert=record.alert,
                risk_event_id=record.risk_event_id,
                scenario_id=record.scenario_id,
            )
            for record in sorted(resend, key=lambda r: r.alert_seq)
        ]

    def status(self, now_mono_ns: int) -> dict[str, object]:
        """SPEC 7.5 status body. No alert text or tokens."""
        pending = sorted(
            (r for r in self.dispatcher.records.values() if r.state not in ("played", "expired")),
            key=lambda r: r.alert_seq,
        )
        return {
            "session_id": self.session_id,
            "device_id": self.device_id,
            "scenarios": [
                {"scenario_id": scenario_id, "state": state}
                for scenario_id, state in self.engine.states().items()
            ],
            "streams": [
                {
                    "stream_id": stream_id,
                    "last_received_age_ms": (
                        None
                        if stream_id not in self.last_received_mono_ns
                        else (now_mono_ns - self.last_received_mono_ns[stream_id]) // 1_000_000
                    ),
                    "dropped_frames": self.input.dropped.get(stream_id, 0),
                }
                for stream_id in sorted(self.inputs)
            ],
            "pending_alerts": [
                {
                    "alert_id": record.alert_id,
                    "alert_seq": record.alert_seq,
                    "delivery_state": record.state,
                    "valid_until_utc": _utc_text(record.alert.valid_until_utc),
                }
                for record in pending
            ],
        }

    def mark_sent(self, alert_id: str, now_mono_ns: int) -> None:
        """Called after the alert message was actually written to the socket."""
        if self.dispatcher.mark_sent(alert_id):
            self._persist_alert(self.dispatcher.records[alert_id], now_mono_ns)

    def ack(self, alert_id: str, status: AckStatus, now_mono_ns: int) -> AckOutcome:
        outcome = self.dispatcher.ack(alert_id, status)
        if outcome.changed:
            self._persist_alert(self.dispatcher.records[alert_id], now_mono_ns)
        return outcome

    def _persist_alert(self, record: AlertRecord, now_mono_ns: int) -> None:
        if self.store is None:
            return
        self.store.save_alert(
            record.alert.model_copy(update={"delivery_state": record.state}),
            session_id=self.session_id,
            risk_event_id=record.risk_event_id,
            scenario_id=record.scenario_id,
            alert_seq=record.alert_seq,
            updated_at_utc=self.clock.at(now_mono_ns),
        )

    def _observation(self, draft: ObservationDraft, now_mono_ns: int) -> Observation:
        return Observation.from_draft(
            draft,
            observation_id=self.ids.new("obs"),
            session_id=self.session_id,
            device_id=self.device_id,
            server_received_mono_ns=now_mono_ns,
            received_at_utc=self.clock.at(now_mono_ns),
        )

    def _decisions(self, decisions: list[RiskDecision], now_mono_ns: int) -> list[PushMessage]:
        messages: list[PushMessage] = []
        for decision in decisions:
            if self.store is not None:
                self.store.save_decision(decision)
            messages.append(DecisionMessage(decision=decision))
            if self.notices.should_notify(decision):
                messages.append(
                    NoticeMessage(
                        notice_id=self.ids.new("notice"),
                        kind="degraded",
                        scenario_id=decision.scenario_id,
                        text=self.degraded_notice_text,
                    )
                )
            if decision.alert is not None:
                self.alert_seq += 1
                record = self.dispatcher.queue(
                    decision, self.alert_seq, now_mono_ns=now_mono_ns, ttl_ms=self.warning_ttl_ms
                )
                self._persist_alert(record, now_mono_ns)
                messages.append(
                    AlertMessage(
                        alert_seq=self.alert_seq,
                        alert=decision.alert,
                        risk_event_id=decision.risk_event_id,
                        scenario_id=decision.scenario_id,
                    )
                )
        return messages
