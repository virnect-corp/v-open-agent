"""Virtual glass outputs: HUD, speaker and haptic as an event log (SPEC 7.4; DEV-02, DEV-03).

The simulator renders an alert or notice by appending one event per declared
output. Events carry IDs only, never alert or notice text, so the log can be
printed or captured without leaking content (DAT-01). Rendering is instantaneous:
the virtual speaker has no duration, so alert-over-notice interruption does not occur.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from voa.contracts.enums import RiskState
from voa.contracts.messages import AlertMessage, NoticeMessage

OutputName = Literal["hud", "speaker", "haptic"]  # SPEC 7.4 OutputCap
OUTPUT_NAMES: tuple[OutputName, ...] = ("hud", "speaker", "haptic")

# Built into the device; never received from the server (SPEC 7.4 watchdog).
DISCONNECT_TEXT = "서버 연결이 끊겨 경고를 받을 수 없습니다."

EventKind = Literal[
    "alert",  # alert rendered on `output`
    "notice",  # degraded notice rendered on `output`
    "degraded_cleared",  # HUD status "감지 제한" removed after a heartbeat
    "alert_cleared",  # HUD alert removed once valid_until passed (server time)
    "disconnect",  # built-in disconnect text rendered on `output`
    "connection_restored",  # HUD only, no speech
    "ack_received",
    "ack_played",
]


@dataclass(frozen=True)
class OutputEvent:
    kind: EventKind
    output: OutputName | None = None
    ref_id: str | None = None  # alert_id, notice_id or scenario_id

    def as_dict(self) -> dict[str, str | None]:
        return {"kind": self.kind, "output": self.output, "ref_id": self.ref_id}


@dataclass
class VirtualOutputs:
    outputs: tuple[OutputName, ...]
    events: list[OutputEvent] = field(default_factory=list)
    played_alert_ids: set[str] = field(default_factory=set)
    hud_alert_id: str | None = None
    hud_alert_valid_until: datetime | None = None
    hud_degraded: set[str] = field(default_factory=set)  # scenario IDs shown as "감지 제한"

    def __post_init__(self) -> None:
        if not self.outputs:
            raise ValueError("at least one output is required")
        if len(set(self.outputs)) != len(self.outputs):
            raise ValueError("outputs must be unique")

    def record(self, kind: EventKind, ref_id: str | None = None) -> None:
        self.events.append(OutputEvent(kind, None, ref_id))

    def is_played(self, alert_id: str) -> bool:
        return alert_id in self.played_alert_ids

    def render_alert(self, message: AlertMessage) -> bool:
        """Render once per alert_id on every declared output; False for a repeat."""
        alert_id = message.alert.alert_id
        if alert_id in self.played_alert_ids:
            return False
        for output in self.outputs:
            self.events.append(OutputEvent("alert", output, alert_id))
        if "hud" in self.outputs:
            self.hud_alert_id = alert_id  # kept until the next alert or valid_until
            self.hud_alert_valid_until = message.alert.valid_until_utc
        self.played_alert_ids.add(alert_id)
        return True

    def render_notice(self, message: NoticeMessage) -> None:
        for output in self.outputs:
            self.events.append(OutputEvent("notice", output, message.notice_id))
        if "hud" in self.outputs:
            self.hud_degraded.add(message.scenario_id)

    def on_heartbeat(self, states: Mapping[str, RiskState], server_time_utc: datetime) -> None:
        """Clear the HUD alert past valid_until and "감지 제한" once no longer unknown."""
        until = self.hud_alert_valid_until
        if self.hud_alert_id is not None and until is not None and server_time_utc >= until:
            self.events.append(OutputEvent("alert_cleared", "hud", self.hud_alert_id))
            self.hud_alert_id = None
            self.hud_alert_valid_until = None
        for scenario_id in sorted(self.hud_degraded):
            if states.get(scenario_id, "unknown") != "unknown":
                self.hud_degraded.discard(scenario_id)
                self.events.append(OutputEvent("degraded_cleared", "hud", scenario_id))

    def announce_disconnect(self) -> None:
        for output in self.outputs:
            self.events.append(OutputEvent("disconnect", output))

    def show_restored(self) -> None:
        if "hud" in self.outputs:
            self.events.append(OutputEvent("connection_restored", "hud"))

    def count(self, kind: EventKind, ref_id: str | None = None) -> int:
        return sum(
            1
            for event in self.events
            if event.kind == kind and (ref_id is None or event.ref_id == ref_id)
        )
