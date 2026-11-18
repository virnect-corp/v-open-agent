"""Replay a parsed fixture through a DecisionEngine and compare with expectations (SPEC 5).

Step timing is measurement only (NFR-02): it is collected beside the decisions
and never feeds back into them, so output stays byte-identical (INV-05).
"""

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from voa.contracts.decision import RiskDecision
from voa.engine.engine import DecisionEngine
from voa.engine.ports import FixedEpochClock, SequentialIdFactory
from voa.engine.profile import PolicyProfile
from voa.simulator.fixture import (
    EXPECTED_FIELDS,
    FIXTURE_DEVICE_ID,
    FIXTURE_SESSION_ID,
    ReplayEvent,
    ReplayObservation,
    decision_view,
)


@dataclass(frozen=True)
class ReplayResult:
    decisions: list[RiskDecision]
    step_ns: list[int]  # one sample per engine.step(); ticks are not timed


def run_events(
    events: Sequence[ReplayEvent],
    profile: PolicyProfile,
    *,
    timer: Callable[[], int] = time.perf_counter_ns,
) -> ReplayResult:
    """Fresh engine, SequentialIdFactory and FixedEpochClock per run (SPEC 4.1)."""
    engine = DecisionEngine(
        FIXTURE_SESSION_ID, FIXTURE_DEVICE_ID, profile, SequentialIdFactory(), FixedEpochClock()
    )
    decisions: list[RiskDecision] = []
    step_ns: list[int] = []
    for event in events:
        if isinstance(event, ReplayObservation):
            started = timer()
            result = engine.step(event.observation, event.now_mono_ns)
            step_ns.append(timer() - started)
            decisions += result.decisions
        else:
            decisions += engine.tick(event.now_mono_ns)
    return ReplayResult(decisions, step_ns)


@dataclass(frozen=True)
class Mismatch:
    """1-based position in the expectation; field is None for a missing or extra row."""

    line_no: int
    field: str | None
    expected: object
    actual: object

    def describe(self) -> str:
        prefix = f"mismatch at expected line {self.line_no}"
        if self.field is not None:
            return f"{prefix}: field {self.field}: expected {self.expected!r}, got {self.actual!r}"
        if self.actual is None:
            return f"{prefix}: expected decision missing"
        return f"{prefix}: unexpected extra decision"


def first_mismatch(
    actual: Sequence[RiskDecision], expected: Sequence[dict[str, object]]
) -> Mismatch | None:
    """Compare SPEC 5 fields in order and return the first difference."""
    for index in range(max(len(actual), len(expected))):
        line_no = index + 1
        if index >= len(actual):
            return Mismatch(line_no, None, expected[index], None)
        view = decision_view(actual[index])
        if index >= len(expected):
            return Mismatch(line_no, None, None, view)
        row = expected[index]
        for name in EXPECTED_FIELDS:
            if view.get(name) != row.get(name):
                return Mismatch(line_no, name, row.get(name), view.get(name))
    return None


def percentile(values: Sequence[int], q: int) -> int:
    """Nearest-rank percentile; values must be non-empty."""
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]
