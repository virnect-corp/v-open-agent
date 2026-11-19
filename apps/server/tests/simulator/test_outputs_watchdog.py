"""TASK-22 virtual outputs and watchdog (SPEC 7.4; DEV-02, DEV-03, DEV-04)."""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from voa.contracts.decision import Alert
from voa.contracts.messages import AlertMessage, NoticeMessage
from voa.simulator.outputs import DISCONNECT_TEXT, VirtualOutputs
from voa.simulator.watchdog import DEFAULT_BACKOFF_S, Backoff, Watchdog

MS = 1_000_000
T0 = datetime(2026, 11, 9, tzinfo=UTC)
SIMULATOR = Path(__file__).resolve().parents[2] / "src" / "voa" / "simulator"


def alert(alert_id: str = "alert_1", seq: int = 1, ttl_s: int = 4) -> AlertMessage:
    return AlertMessage(
        alert_seq=seq,
        alert=Alert(
            alert_id=alert_id,
            text="전방 통로에 장애물이 보입니다.",
            text_kind="approved_template",
            delivery_state="queued",
            valid_until_utc=T0 + timedelta(seconds=ttl_s),
        ),
        risk_event_id="risk_1",
        scenario_id="visible_obstacle_in_corridor",
    )


def notice(scenario: str = "visible_obstacle_in_corridor") -> NoticeMessage:
    return NoticeMessage(notice_id="notice_1", kind="degraded", scenario_id=scenario, text="제한")


# --- outputs (DEV-02, DEV-03) ---


def test_alert_renders_once_on_every_declared_output() -> None:
    out = VirtualOutputs(("hud", "speaker", "haptic"))
    assert out.render_alert(alert()) is True
    assert [(e.kind, e.output) for e in out.events] == [
        ("alert", "hud"),
        ("alert", "speaker"),
        ("alert", "haptic"),
    ]
    assert out.hud_alert_id == "alert_1"


def test_repeated_alert_id_is_not_rendered_again() -> None:
    out = VirtualOutputs(("hud", "speaker"))
    out.render_alert(alert())
    assert out.render_alert(alert(seq=2)) is False
    assert out.count("alert", "alert_1") == 2  # hud + speaker from the first time only


def test_undeclared_outputs_are_not_used() -> None:
    out = VirtualOutputs(("speaker",))
    out.render_alert(alert())
    out.render_notice(notice())
    assert {e.output for e in out.events} == {"speaker"}
    assert out.hud_alert_id is None and not out.hud_degraded


def test_no_outputs_or_duplicates_are_rejected() -> None:
    with pytest.raises(ValueError):
        VirtualOutputs(())
    with pytest.raises(ValueError):
        VirtualOutputs(("hud", "hud"))


def test_notice_sets_hud_status_until_heartbeat_shows_not_unknown() -> None:
    out = VirtualOutputs(("hud", "speaker"))
    out.render_notice(notice())
    assert out.count("notice") == 2
    scenario = "visible_obstacle_in_corridor"
    out.on_heartbeat({scenario: "unknown"}, T0)
    assert out.hud_degraded == {scenario}
    out.on_heartbeat({scenario: "clear"}, T0)
    assert not out.hud_degraded
    assert out.count("degraded_cleared", scenario) == 1


def test_hud_alert_is_kept_until_valid_until() -> None:
    out = VirtualOutputs(("hud",))
    out.render_alert(alert(ttl_s=4))
    out.on_heartbeat({}, T0 + timedelta(seconds=3))
    assert out.hud_alert_id == "alert_1"
    out.on_heartbeat({}, T0 + timedelta(seconds=4))  # boundary: valid_until reached
    assert out.hud_alert_id is None
    assert out.count("alert_cleared", "alert_1") == 1


def test_events_carry_ids_but_never_text() -> None:
    out = VirtualOutputs(("hud", "speaker"))
    out.render_alert(alert())
    out.render_notice(notice())
    out.announce_disconnect()
    dumped = repr([e.as_dict() for e in out.events])
    assert "장애물" not in dumped and "제한" not in dumped and DISCONNECT_TEXT not in dumped


def test_restored_status_is_hud_only_and_silent() -> None:
    out = VirtualOutputs(("hud", "speaker"))
    out.show_restored()
    assert [(e.kind, e.output) for e in out.events] == [("connection_restored", "hud")]
    speaker_only = VirtualOutputs(("speaker",))
    speaker_only.show_restored()
    assert speaker_only.events == []


# --- watchdog / backoff (DEV-04, T-R16) ---


def test_watchdog_announces_once_at_the_boundary() -> None:
    dog = Watchdog(3000, last_message_mono_ns=0)
    assert not dog.should_announce(2999 * MS)
    assert dog.should_announce(3000 * MS)
    assert not dog.should_announce(9000 * MS)  # once per silence


def test_watchdog_rearms_after_a_server_message() -> None:
    dog = Watchdog(3000, last_message_mono_ns=0)
    assert dog.should_announce(3000 * MS)
    dog.feed(4000 * MS)
    assert not dog.should_announce(6999 * MS)
    assert dog.should_announce(7000 * MS)
    assert dog.remaining_s(7000 * MS) == 0


def test_watchdog_rejects_non_positive_period() -> None:
    with pytest.raises(ValueError):
        Watchdog(0, last_message_mono_ns=0)


def test_backoff_is_1_2_4_8_then_capped_and_resets() -> None:
    backoff = Backoff()
    assert DEFAULT_BACKOFF_S == (1.0, 2.0, 4.0, 8.0)
    assert [backoff.next_delay() for _ in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
    backoff.reset()
    assert backoff.next_delay() == 1.0
    with pytest.raises(ValueError):
        Backoff(())


# --- module boundary (03-collaboration-architecture 2) ---


def test_simulator_does_not_import_server_or_engine() -> None:
    for path in SIMULATOR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(("voa.server", "voa.engine")), (path.name, name)
