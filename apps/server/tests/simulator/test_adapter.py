"""TASK-22 glass client protocol handling without a server (SPEC 7.1-7.4; DEV-02, T-R10)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from voa.contracts.decision import Alert
from voa.contracts.messages import AlertMessage, HeartbeatMessage
from voa.simulator.__main__ import build_parser
from voa.simulator.__main__ import main as sim_main
from voa.simulator.adapter import ClientConfig, GlassClient, SessionRejected

T0 = datetime(2026, 11, 9, tzinfo=UTC)
CONFIG = ClientConfig(
    server_url="http://127.0.0.1:1",
    device_id="dev_fixture",
    secret=None,
    inputs=("camera_0",),
    outputs=("hud", "speaker"),
)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


def alert(alert_id: str, seq: int) -> AlertMessage:
    return AlertMessage(
        alert_seq=seq,
        alert=Alert(
            alert_id=alert_id,
            text="t",
            text_kind="approved_template",
            delivery_state="queued",
            valid_until_utc=T0 + timedelta(seconds=4),
        ),
        risk_event_id="risk_1",
        scenario_id="visible_obstacle_in_corridor",
    )


def handle(client: GlassClient, ws: FakeSocket, message: Any) -> None:
    asyncio.run(client._handle(ws, message))  # type: ignore[arg-type]


def test_new_alert_acks_received_then_renders_then_played() -> None:
    client, ws = GlassClient(CONFIG), FakeSocket()
    handle(client, ws, alert("alert_a", 1))
    assert [m["status"] for m in ws.sent] == ["received", "played"]
    kinds = [e.kind for e in client.outputs.events]
    assert kinds == ["ack_received", "alert", "alert", "ack_played"]
    assert client.last_alert_seq == 1


def test_repeated_alert_only_resends_played_without_rendering() -> None:
    client, ws = GlassClient(CONFIG), FakeSocket()
    handle(client, ws, alert("alert_a", 1))
    handle(client, ws, alert("alert_a", 1))
    assert [m["status"] for m in ws.sent] == ["received", "played", "played"]
    assert client.outputs.count("alert", "alert_a") == 2  # hud + speaker, once


def test_alert_cursor_never_goes_back() -> None:
    client, ws = GlassClient(CONFIG), FakeSocket()
    handle(client, ws, alert("alert_b", 2))
    handle(client, ws, alert("alert_a", 1))
    assert client.last_alert_seq == 2


def test_restored_status_shown_on_first_heartbeat_after_reconnect_only() -> None:
    client, ws = GlassClient(CONFIG), FakeSocket()
    beat = HeartbeatMessage(server_time_utc=T0, states={})
    handle(client, ws, beat)
    assert client.outputs.count("connection_restored") == 0
    client._restore_pending = True
    handle(client, ws, beat)
    handle(client, ws, beat)
    assert client.outputs.count("connection_restored") == 1


def test_session_refusal_carries_status_and_code_only() -> None:
    def refuse(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return 400, {"code": "no_output_capability"}

    client = GlassClient(CONFIG, http_post=refuse)
    with pytest.raises(SessionRejected) as info:
        asyncio.run(client.start())
    assert (info.value.status, info.value.code) == (400, "no_output_capability")


def test_refresh_401_creates_a_new_session_and_resets_the_cursor() -> None:
    calls: list[str] = []

    def server(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/refresh"):
            return 401, {"code": "unauthorized"}
        n = len(calls)
        return 200, {
            "session_id": f"sess_{n}",
            "token": f"tok_{n}",
            "refresh_token": f"ref_{n}",
            "heartbeat_ms": 100,
            "watchdog_ms": 300,
        }

    client = GlassClient(CONFIG, http_post=server)
    asyncio.run(client._create_session())
    client.last_alert_seq = 5
    asyncio.run(client._refresh_or_recreate())
    assert calls == ["sessions", "refresh", "sessions"]
    assert client.session_id == "sess_3"
    assert client.last_alert_seq == 0 and client.sessions_created == 2


def test_refresh_success_rotates_tokens_and_keeps_the_session() -> None:
    def server(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if url.endswith("/refresh"):
            assert body == {"refresh_token": "ref_0"}
            return 200, {"token": "tok_1", "refresh_token": "ref_1"}
        return 200, {
            "session_id": "sess_0",
            "token": "tok_0",
            "refresh_token": "ref_0",
            "heartbeat_ms": 100,
            "watchdog_ms": 300,
        }

    client = GlassClient(CONFIG, http_post=server)
    asyncio.run(client._create_session())
    client.last_alert_seq = 3
    asyncio.run(client._refresh_or_recreate())
    assert client.session_id == "sess_0" and client.last_alert_seq == 3
    assert client._credentials is not None and client._credentials.token == "tok_1"


def test_offline_observation_is_dropped_not_buffered() -> None:
    client = GlassClient(CONFIG)
    assert asyncio.run(client.report_health("camera_0", "down", "camera_error")) is False


def test_ws_url_follows_the_http_scheme() -> None:
    assert CONFIG.ws_url("sess_1") == "ws://127.0.0.1:1/v1/streams/sess_1"
    tls = ClientConfig("https://edge:8765/", "d", None, ("camera_0",), ("hud",))
    assert tls.ws_url("s") == "wss://edge:8765/v1/streams/s"


def test_cli_rejects_unknown_outputs_and_missing_fixture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--fixture", "f", "--outputs", "hud,screen"])
    assert sim_main(["--fixture", "/nonexistent/fixture.jsonl"]) == 2
    assert "nonexistent" not in capsys.readouterr().err
