"""Device-side connection watchdog and reconnect backoff (SPEC 7.4; DEV-04, T-R16).

Only the device can tell that the server is gone, so the device announces it:
after `watchdog_ms` without any server message, the built-in disconnect text is
rendered once. The watchdog re-arms only when a server message arrives again.
Time is passed in (monotonic ns), so these classes are deterministic to test.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

DEFAULT_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)  # then 8 s forever


@dataclass
class Watchdog:
    watchdog_ms: int
    last_message_mono_ns: int
    announced: bool = False

    def __post_init__(self) -> None:
        if self.watchdog_ms <= 0:
            raise ValueError("watchdog_ms must be positive")

    def feed(self, now_mono_ns: int) -> None:
        """Any server message (heartbeat or other) proves the link is alive."""
        self.last_message_mono_ns = now_mono_ns
        self.announced = False

    def expired(self, now_mono_ns: int) -> bool:
        return now_mono_ns - self.last_message_mono_ns >= self.watchdog_ms * 1_000_000

    def should_announce(self, now_mono_ns: int) -> bool:
        """True exactly once per silence episode, at or after `watchdog_ms`."""
        if self.announced or not self.expired(now_mono_ns):
            return False
        self.announced = True
        return True

    def remaining_s(self, now_mono_ns: int) -> float:
        left = self.last_message_mono_ns + self.watchdog_ms * 1_000_000 - now_mono_ns
        return max(left, 0) / 1e9


@dataclass
class Backoff:
    """Reconnect delays 1/2/4/8 s, then the last value; reset after a success."""

    delays_s: Sequence[float] = DEFAULT_BACKOFF_S
    _attempt: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.delays_s or any(delay <= 0 for delay in self.delays_s):
            raise ValueError("delays must be non-empty and positive")

    def next_delay(self) -> float:
        delay = self.delays_s[min(self._attempt, len(self.delays_s) - 1)]
        self._attempt += 1
        return delay

    def reset(self) -> None:
        self._attempt = 0
