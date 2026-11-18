"""Scenario fixture parser and replay event builder (SPEC 5, INP-01).

A fixture is JSONL with one event per line: `{"at_ms", "obs": <Draft>}` or
`{"at_ms", "tick": true}`. This module fills the server fields deterministically
and returns timed events; driving a DecisionEngine is the caller's job
(tests, `voa.replay`), so the simulator never depends on the engine.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError, model_validator

from voa.contracts.decision import ReasonCode, RiskDecision
from voa.contracts.enums import RiskState
from voa.contracts.observation import (
    AsrPayload,
    Detection,
    DetectionPayload,
    HealthPayload,
    ModelRef,
    Observation,
    ObservationDraft,
    OcrPayload,
    SoundEventPayload,
    Track,
    TrackPayload,
)
from voa.contracts.types import MODEL_CONFIG, Id

FIXTURE_SESSION_ID = "sess_fixture"
FIXTURE_DEVICE_ID = "dev_fixture"
EXPECTED_FIELDS = (
    "scenario_id",
    "previous_state",
    "state",
    "reason_codes",
    "evidence_ids",
    "risk_event_id",
    "alert_id",
)


class UtcAt(Protocol):
    def at(self, mono_ns: int) -> datetime: ...


class FixtureError(ValueError):
    """A fixture or expectation line is invalid. The message never echoes values."""

    def __init__(self, line_no: int, reason: str) -> None:
        super().__init__(f"line {line_no}: {reason}")
        self.line_no = line_no


@dataclass(frozen=True)
class ReplayObservation:
    line_no: int
    now_mono_ns: int
    observation: Observation


@dataclass(frozen=True)
class ReplayTick:
    line_no: int
    now_mono_ns: int


ReplayEvent = ReplayObservation | ReplayTick


class _FixtureLine(BaseModel):
    model_config = MODEL_CONFIG
    at_ms: int = Field(ge=0)
    obs: ObservationDraft | None = None
    tick: Literal[True] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "_FixtureLine":
        if (self.obs is None) == (self.tick is None):
            raise ValueError("exactly one of obs or tick is required")
        return self


class _ExpectedLine(BaseModel):
    model_config = MODEL_CONFIG
    scenario_id: Id
    previous_state: RiskState | None
    state: RiskState
    reason_codes: list[ReasonCode]
    evidence_ids: list[Id]
    risk_event_id: Id
    alert_id: Id | None = None


_KNOWN_LOCATIONS = frozenset(
    name
    for model in (
        _FixtureLine,
        _ExpectedLine,
        ObservationDraft,
        ModelRef,
        Detection,
        DetectionPayload,
        Track,
        TrackPayload,
        SoundEventPayload,
        AsrPayload,
        OcrPayload,
        HealthPayload,
    )
    for name in model.model_fields
) | {
    "object_detection",
    "track_state",
    "sound_event",
    "asr_transcript",
    "ocr_text",
    "health",
}


def _reason(error: ValidationError) -> str:
    """Locations and error types only; unknown keys and inputs are masked."""
    parts = []
    for item in error.errors(include_input=False, include_context=False, include_url=False):
        location = ".".join(
            str(part) if isinstance(part, int) or part in _KNOWN_LOCATIONS else "[unknown]"
            for part in item["loc"]
        )
        parts.append(f"{location or 'root'}:{item['type']}")
    return ", ".join(sorted(set(parts)))


def _rows(lines: Iterable[str]) -> Iterable[tuple[int, object]]:
    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise FixtureError(line_no, "invalid JSON") from None
        if not isinstance(data, dict):
            raise FixtureError(line_no, "line must be a JSON object")
        yield line_no, data


def parse_fixture(lines: Iterable[str], clock: UtcAt) -> list[ReplayEvent]:
    """Fill observation_id obs_NNNN (0-based, observations only) and server time."""
    events: list[ReplayEvent] = []
    last_at_ms = 0
    count = 0
    for line_no, data in _rows(lines):
        try:
            row = _FixtureLine.model_validate(data)
        except ValidationError as error:
            raise FixtureError(line_no, _reason(error)) from None
        if row.at_ms < last_at_ms:
            raise FixtureError(line_no, "at_ms must be non-decreasing")
        last_at_ms = row.at_ms
        now_mono_ns = row.at_ms * 1_000_000
        if row.obs is None:
            events.append(ReplayTick(line_no, now_mono_ns))
            continue
        observation = Observation.from_draft(
            row.obs,
            observation_id=f"obs_{count:04d}",
            session_id=FIXTURE_SESSION_ID,
            device_id=FIXTURE_DEVICE_ID,
            server_received_mono_ns=now_mono_ns,
            received_at_utc=clock.at(now_mono_ns),
        )
        count += 1
        events.append(ReplayObservation(line_no, now_mono_ns, observation))
    return events


def load_fixture(path: str | Path, clock: UtcAt) -> list[ReplayEvent]:
    with Path(path).open(encoding="utf-8") as handle:
        return parse_fixture(handle, clock)


def decision_view(decision: RiskDecision) -> dict[str, object]:
    """The SPEC 5 comparison fields; alert_id appears only when there is an alert."""
    view: dict[str, object] = {
        "scenario_id": decision.scenario_id,
        "previous_state": decision.previous_state,
        "state": decision.state,
        "reason_codes": list(decision.reason_codes),
        "evidence_ids": list(decision.evidence_ids),
        "risk_event_id": decision.risk_event_id,
    }
    if decision.alert is not None:
        view["alert_id"] = decision.alert.alert_id
    return view


def parse_expected(lines: Iterable[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_no, data in _rows(lines):
        try:
            row = _ExpectedLine.model_validate(data)
        except ValidationError as error:
            raise FixtureError(line_no, _reason(error)) from None
        view = row.model_dump()
        if view["alert_id"] is None:
            del view["alert_id"]
        rows.append(view)
    return rows


def load_expected(path: str | Path) -> list[dict[str, object]]:
    with Path(path).open(encoding="utf-8") as handle:
        return parse_expected(handle)
