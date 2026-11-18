"""TASK-16 operational ID and UTC ports (SPEC 4.1, ADR-0001)."""

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from voa.contracts.types import Id  # noqa: F401  (documents the target ID rule)
from voa.engine.ports import IdFactory, UtcClock
from voa.server.runtime import AnchoredUtcClock, SecureIdFactory

ID_RULE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@pytest.mark.parametrize("prefix", ["risk", "alert", "obs", "sess", "notice"])
def test_secure_ids_have_prefix_and_token(prefix: str) -> None:
    factory = SecureIdFactory()
    value = factory.new(prefix)  # type: ignore[arg-type]
    head, token = value.split("_", 1)
    assert head == prefix
    assert len(token) == 24  # secrets.token_urlsafe(18) -> 24 URL-safe characters
    assert ID_RULE.match(value)


def test_secure_ids_do_not_repeat() -> None:
    factory = SecureIdFactory()
    values = {factory.new("risk") for _ in range(2000)}
    assert len(values) == 2000


def test_ports_satisfy_engine_protocols() -> None:
    ids: IdFactory = SecureIdFactory()
    clock: UtcClock = AnchoredUtcClock(datetime(2026, 11, 9, tzinfo=UTC), 0)
    assert ids.new("obs").startswith("obs_")
    assert clock.at(0) == datetime(2026, 11, 9, tzinfo=UTC)


def test_anchored_clock_maps_monotonic_offsets_to_utc() -> None:
    anchor = datetime(2026, 11, 9, 3, 0, tzinfo=UTC)
    clock = AnchoredUtcClock(anchor, anchor_mono_ns=5_000_000_000)
    assert clock.at(5_000_000_000) == anchor
    assert clock.at(5_001_500_000) == anchor + timedelta(milliseconds=1, microseconds=500)
    assert clock.at(5_000_000_999) == anchor  # truncated to microseconds
    assert clock.at(4_000_000_000) == anchor - timedelta(seconds=1)


def test_anchored_clock_requires_utc_anchor() -> None:
    with pytest.raises(ValueError):
        AnchoredUtcClock(datetime(2026, 11, 9), 0)
    with pytest.raises(ValueError):
        AnchoredUtcClock(datetime(2026, 11, 9, tzinfo=timezone(timedelta(hours=9))), 0)


def test_anchored_clock_now_uses_current_time() -> None:
    before = datetime.now(UTC)
    clock = AnchoredUtcClock.now()
    after = datetime.now(UTC)
    assert before <= clock.anchor_utc <= after
    assert clock.anchor_mono_ns > 0
