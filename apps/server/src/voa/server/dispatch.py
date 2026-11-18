"""Alert delivery state and degraded notices (SPEC 7.3, 7.4; COM-03, DEV-03).

Delivery order is queued < sent < received < played and never goes back.
Past valid_until, queued becomes expired and sent becomes unknown_delivery;
received keeps waiting for played. A late ACK after expired/unknown_delivery
restores the acknowledged state and is logged as `late_ack`.
RiskDecision stays the immutable creation record; only this module tracks delivery.
"""

import logging
from dataclasses import dataclass, field
from typing import Literal

from voa.contracts.decision import Alert, RiskDecision
from voa.contracts.enums import DeliveryState, RiskState

AckStatus = Literal["received", "played"]
_RANK: dict[DeliveryState, int] = {"queued": 0, "sent": 1, "received": 2, "played": 3}
_TERMINAL_WITHOUT_ACK: frozenset[DeliveryState] = frozenset({"expired", "unknown_delivery"})
_NOTICE_FROM: frozenset[RiskState] = frozenset({"candidate", "warning"})

log = logging.getLogger("voa.server.dispatch")


@dataclass
class AlertRecord:
    alert: Alert
    alert_seq: int
    scenario_id: str
    risk_event_id: str
    valid_until_mono_ns: int
    state: DeliveryState = "queued"

    @property
    def alert_id(self) -> str:
        return self.alert.alert_id


@dataclass(frozen=True)
class AckOutcome:
    changed: bool
    late: bool = False


@dataclass
class AlertDispatcher:
    records: dict[str, AlertRecord] = field(default_factory=dict)

    def queue(
        self, decision: RiskDecision, alert_seq: int, *, now_mono_ns: int, ttl_ms: int
    ) -> AlertRecord:
        assert decision.alert is not None
        record = AlertRecord(
            alert=decision.alert,
            alert_seq=alert_seq,
            scenario_id=decision.scenario_id,
            risk_event_id=decision.risk_event_id,
            valid_until_mono_ns=now_mono_ns + ttl_ms * 1_000_000,
        )
        self.records[record.alert_id] = record
        return record

    def mark_sent(self, alert_id: str) -> bool:
        record = self.records.get(alert_id)
        if record is None or record.state != "queued":
            return False
        record.state = "sent"
        return True

    def ack(self, alert_id: str, status: AckStatus) -> AckOutcome:
        record = self.records.get(alert_id)
        if record is None:
            return AckOutcome(changed=False)
        if record.state in _TERMINAL_WITHOUT_ACK:
            record.state = status
            log.info("", extra={"event": "late_ack", "alert_id": alert_id, "status": status})
            return AckOutcome(changed=True, late=True)
        # Equal or lower rank (duplicates, regressions, anything after played) is ignored.
        if _RANK[status] <= _RANK[record.state]:
            return AckOutcome(changed=False)
        record.state = status
        return AckOutcome(changed=True)

    def expire(self, now_mono_ns: int) -> list[AlertRecord]:
        changed = []
        for record in self.records.values():
            if now_mono_ns < record.valid_until_mono_ns:
                continue
            if record.state == "queued":
                record.state = "expired"
                changed.append(record)
            elif record.state == "sent":
                record.state = "unknown_delivery"
                changed.append(record)
        return changed


@dataclass
class NoticeTracker:
    """One degraded notice per scenario when candidate/warning becomes unknown (T-R18)."""

    _sent: set[str] = field(default_factory=set)

    def should_notify(self, decision: RiskDecision) -> bool:
        scenario = decision.scenario_id
        if decision.state != "unknown":
            self._sent.discard(scenario)  # leaving unknown re-arms the notice
            return False
        if decision.previous_state not in _NOTICE_FROM or scenario in self._sent:
            return False
        self._sent.add(scenario)
        return True
