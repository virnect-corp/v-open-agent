"""Frames -> provider -> DecisionEngine without a server (ADR-0007 section 6; NFR-02, NFR-03).

Time comes from the frame timestamps and a fixed-epoch clock, IDs are sequential,
so decisions are byte-identical across runs (INV-05). Like the server's tick loop
(VOA_TICK_INTERVAL_MS), `engine.tick` runs every `tick_ms` between frames, so a
gap between frames turns scenarios `unknown` exactly as it would live. Latency is measurement only:
it is collected beside the decisions and never feeds back into them.

`unknown_rate` = processed frames after which at least one scenario was `unknown`,
divided by processed frames. Recall, false alerts per hour and end-to-end latency
need ground truth or the server path, so they stay `TBD` (AGENTS rule 10).
"""

import os
import platform
import time
from collections.abc import Callable, Iterable, Sized
from dataclasses import asdict, dataclass, field

from voa.contracts.decision import RiskDecision
from voa.contracts.observation import Observation
from voa.engine.engine import DecisionEngine
from voa.engine.ports import FixedEpochClock, SequentialIdFactory
from voa.engine.profile import PolicyProfile
from voa.providers.rtdetr import Frame, RtDetrProvider
from voa.replay.runner import percentile

EVAL_SESSION_ID = "sess_eval"
EVAL_DEVICE_ID = "dev_eval"
TBD = "TBD"


@dataclass(frozen=True)
class EvalReport:
    """PLAN section 4 evaluation report plus the C0 measurements."""

    run_id: str
    code_commit: str
    profile: str
    model_artifacts: list[dict[str, str]]
    hardware: dict[str, object]
    dataset: dict[str, object]
    metrics: dict[str, object]
    measurements: dict[str, object]
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class EvalResult:
    decisions: list[RiskDecision]
    report: EvalReport


def _ms(samples: list[int]) -> dict[str, object]:
    if not samples:
        return {"n": 0, "p50": TBD, "p95": TBD}
    return {
        "n": len(samples),
        "p50": round(percentile(samples, 50) / 1e6, 3),
        "p95": round(percentile(samples, 95) / 1e6, 3),
    }


def _us(samples: list[int]) -> dict[str, object]:
    if not samples:
        return {"n": 0, "p50": TBD, "p95": TBD}
    return {
        "n": len(samples),
        "p50": percentile(samples, 50) // 1000,
        "p95": percentile(samples, 95) // 1000,
    }


def _hardware() -> dict[str, object]:
    return {
        "machine": platform.machine(),
        "system": platform.system(),
        "cpu_count": os.cpu_count(),
        "accelerator": "none",
    }


def run_eval(
    frames: Iterable[Frame],
    provider: RtDetrProvider,
    profile: PolicyProfile,
    *,
    dataset_name: str | None = None,
    fps: float | None = None,
    sha256: str | None = None,
    run_id: str = "local",
    code_commit: str = TBD,
    tick_ms: int = 100,
    timer: Callable[[], int] = time.perf_counter_ns,
) -> EvalResult:
    if tick_ms <= 0:
        raise ValueError("tick_ms must be positive")
    tick_ns = tick_ms * 1_000_000
    next_tick = tick_ns
    clock = FixedEpochClock()
    engine = DecisionEngine(EVAL_SESSION_ID, EVAL_DEVICE_ID, profile, SequentialIdFactory(), clock)
    decisions: list[RiskDecision] = []
    provider_ns: list[int] = []
    step_ns: list[int] = []
    processed = unknown_frames = observations = 0
    for frame in frames:
        while next_tick < frame.capture_ts_monotonic_ns:
            decisions += engine.tick(next_tick)
            next_tick += tick_ns
        started = timer()
        drafts = provider.process(frame)
        provider_ns.append(timer() - started)
        now = frame.capture_ts_monotonic_ns
        for draft in drafts:
            observation = Observation.from_draft(
                draft,
                observation_id=f"obs_{observations:04d}",
                session_id=EVAL_SESSION_ID,
                device_id=EVAL_DEVICE_ID,
                server_received_mono_ns=now,
                received_at_utc=clock.at(now),
            )
            observations += 1
            started = timer()
            result = engine.step(observation, now)
            step_ns.append(timer() - started)
            decisions += result.decisions
        processed += 1
        unknown_frames += any(state == "unknown" for state in engine.states().values())

    total = len(frames) if isinstance(frames, Sized) else processed
    skipped = getattr(frames, "skipped", 0)
    report = EvalReport(
        run_id=run_id,
        code_commit=code_commit,
        profile=f"{profile.profile_id}@{profile.profile_version}",
        model_artifacts=[
            {"id": provider.model_id, "revision": provider.revision, "sha256": sha256 or TBD}
        ],
        hardware=_hardware(),
        dataset={
            "name": dataset_name or getattr(frames, "name", TBD),
            "frames": total,
            "fps": fps if fps is not None else getattr(frames, "fps", TBD),
        },
        metrics={
            "event_recall": TBD,
            "false_alerts_per_hour": TBD,
            "e2e_p95_ms": TBD,
            "unknown_rate": round(unknown_frames / processed, 4) if processed else TBD,
            "unsupported_claim_rate": TBD,
        },
        measurements={
            "provider_ms": _ms(provider_ns),
            "step_us": _us(step_ns),
            "frames": processed,
            "skipped_frames": skipped,
            "decisions": len(decisions),
            "alerts": sum(1 for decision in decisions if decision.alert is not None),
        },
        limitations=[
            "CPU only: Docker Linux arm64 without GPU (ADR-0007)",
            "no ground truth: event_recall and false_alerts_per_hour not measured",
            "no server path: e2e latency not measured",
        ],
    )
    return EvalResult(decisions, report)
