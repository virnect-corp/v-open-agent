"""Operational ID and UTC ports for the server (SPEC 4.1, ADR-0001).

The engine only knows the IdFactory/UtcClock protocols. Tests and replay use
SequentialIdFactory/FixedEpochClock; the server injects these implementations.
"""

import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from voa.engine.ports import IdPrefix


class SecureIdFactory:
    """`{prefix}_{secrets.token_urlsafe(18)}`; share one instance per server process."""

    def new(self, prefix: IdPrefix) -> str:
        return f"{prefix}_{secrets.token_urlsafe(18)}"


@dataclass(frozen=True)
class AnchoredUtcClock:
    """Map server monotonic ns to UTC from one (UTC, monotonic) anchor taken at start."""

    anchor_utc: datetime
    anchor_mono_ns: int

    def __post_init__(self) -> None:
        if self.anchor_utc.utcoffset() != timedelta(0):
            raise ValueError("anchor must be timezone-aware UTC")

    @classmethod
    def now(cls) -> "AnchoredUtcClock":
        return cls(datetime.now(UTC), time.monotonic_ns())

    def at(self, mono_ns: int) -> datetime:
        return self.anchor_utc + timedelta(microseconds=(mono_ns - self.anchor_mono_ns) // 1000)
