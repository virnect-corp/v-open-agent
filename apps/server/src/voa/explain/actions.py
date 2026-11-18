"""Allowlisted actions with deterministic idempotency keys (SPEC 6, EXP-02).

Anything outside the allowlist has no execution path. execute() only returns a
request record; delivery to the device is the server's job (Stage B). Untrusted
text (OCR, environment speech, VLM output) must never reach execute() as an
action name (AGENTS rule 6).
"""

import hashlib
from dataclasses import dataclass
from typing import Final

ALLOWED_ACTIONS: Final = frozenset(
    {
        "play_alert",
        "show_text",
        "repeat_last_verified_alert",
        "acknowledge_alert",
        "export_metadata_if_authorized",
    }
)


class ForbiddenActionError(ValueError):
    """The message never echoes the requested name, which may be untrusted text."""

    def __init__(self) -> None:
        super().__init__("action is not in the allowlist")


def idempotency_key(
    *, device_id: str, scenario_id: str, risk_event_id: str, alert_id: str, action_kind: str
) -> str:
    joined = "|".join([device_id, scenario_id, risk_event_id, alert_id, action_kind])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActionRequest:
    action_kind: str
    device_id: str
    scenario_id: str
    risk_event_id: str
    alert_id: str
    idempotency_key: str


class ActionExecutor:
    def execute(
        self,
        action_kind: str,
        *,
        device_id: str,
        scenario_id: str,
        risk_event_id: str,
        alert_id: str,
    ) -> ActionRequest:
        """Exact-match allowlist; case and whitespace variants are forbidden."""
        if action_kind not in ALLOWED_ACTIONS:
            raise ForbiddenActionError
        return ActionRequest(
            action_kind=action_kind,
            device_id=device_id,
            scenario_id=scenario_id,
            risk_event_id=risk_event_id,
            alert_id=alert_id,
            idempotency_key=idempotency_key(
                device_id=device_id,
                scenario_id=scenario_id,
                risk_event_id=risk_event_id,
                alert_id=alert_id,
                action_kind=action_kind,
            ),
        )
