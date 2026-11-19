"""TASK-22 simulator = glass client against a real uvicorn server (UC-01..05, 08, 09; T-R16).

Each test starts the app on 127.0.0.1 (`live_server`).
Time is real here, so assertions use state sequences and output logs, not IDs or
exact timestamps. Heartbeat 100 ms / watchdog 300 ms keep the run short.
"""

import asyncio
import time
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.live_server import (
    SCENARIOS,
    SECRET,
    WATCHDOG_MS,
    LiveServer,
    make_workdir,
)
from voa.simulator.__main__ import main as sim_main
from voa.simulator.adapter import ClientConfig, GlassClient, SessionRejected
from voa.simulator.fixture import TimedDraft, load_drafts
from voa.simulator.outputs import OutputName

OBSTACLE = "visible_obstacle_in_corridor"
MOVING = "moving_object_in_attention_zone"
FAST_BACKOFF = (0.05, 0.1, 0.2, 0.4)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    live = LiveServer(make_workdir(tmp_path))
    live.start()
    yield live
    live.stop()


def client_for(
    live: LiveServer,
    outputs: tuple[OutputName, ...] = ("hud", "speaker"),
    device: str = "dev_example",
) -> GlassClient:
    """`dev_example` allows hud/speaker; the insecure dev device allows every capability."""
    return GlassClient(
        ClientConfig(
            server_url=live.url,
            device_id=device,
            secret=SECRET if device == "dev_example" else None,
            inputs=("camera_0",),
            outputs=outputs,
            backoff_s=FAST_BACKOFF,
        )
    )


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


async def started(live: LiveServer, **kwargs: Any) -> GlassClient:
    client = client_for(live, **kwargs)
    await client.start()
    await asyncio.wait_for(client.connected.wait(), 5)
    return client


def fixture(name: str) -> list[TimedDraft]:
    return load_drafts(SCENARIOS / f"{name}.jsonl")


def transitions(client: GlassClient, scenario: str) -> list[tuple[str | None, str]]:
    return [(d.previous_state, d.state) for d in client.decisions if d.scenario_id == scenario]


def alert_ids(client: GlassClient) -> list[str]:
    return sorted({e.ref_id for e in client.outputs.events if e.kind == "alert" and e.ref_id})


async def status(live: LiveServer, client: GlassClient) -> dict[str, Any]:
    assert client._credentials is not None
    async with httpx.AsyncClient(trust_env=False) as http:
        response = await http.get(
            f"{live.url}/v1/sessions/{client.session_id}/status",
            headers={"Authorization": f"Bearer {client._credentials.token}"},
        )
    assert response.status_code == 200
    return dict(response.json())


# --- UC-01 / COM-02 / DEV-02: warning alert rendered once, ACK timing ---


def test_uc01_alert_is_rendered_on_all_outputs_and_acked(server: LiveServer) -> None:
    async def scenario() -> None:
        client = await started(server)
        try:
            await client.play(fixture("uc01_obstacle"))
            await until(lambda: client.outputs.count("ack_played") == 1)
            assert ("candidate", "warning") in transitions(client, OBSTACLE)
            [alert_id] = alert_ids(client)
            assert [(e.kind, e.output) for e in client.outputs.events if e.ref_id == alert_id] == [
                ("ack_received", None),
                ("alert", "hud"),
                ("alert", "speaker"),
                ("ack_played", None),
            ]
            body = await status(server, client)
            assert body["pending_alerts"] == []  # played is no longer pending (SPEC 7.5)
            assert {"scenario_id": OBSTACLE, "state": "warning"} in body["scenarios"]
        finally:
            await client.stop()

    run(scenario)


# --- UC-02: moving person, then camera motion uncompensated -> unknown + notice ---


def test_uc02_moving_person_warns_then_motion_blocks_to_unknown(server: LiveServer) -> None:
    async def scenario() -> None:
        client = await started(server, outputs=("hud", "speaker", "haptic"), device="dev_fixture")
        try:
            await client.play(fixture("uc02_moving_person"))
            await until(lambda: ("warning", "unknown") in transitions(client, MOVING), 3)
            assert ("candidate", "warning") in transitions(client, MOVING)
            assert len(alert_ids(client)) == 1
            assert client.outputs.count("alert") == 3  # hud, speaker, haptic
            assert client.outputs.count("notice") == 3  # one notice on each output
            assert MOVING in client.outputs.hud_degraded
        finally:
            await client.stop()

    run(scenario)


# --- UC-03 / T-R18: input lost during warning -> unknown, never resolved; notice once ---


def test_uc03_input_lost_becomes_unknown_with_one_notice(server: LiveServer) -> None:
    async def scenario() -> None:
        client = await started(server)
        try:
            await client.play(fixture("uc03_input_lost"))
            await until(lambda: ("warning", "unknown") in transitions(client, OBSTACLE), 3)
            await asyncio.sleep(0.5)  # later ticks must not notify again
            assert ("warning", "resolved") not in transitions(client, OBSTACLE)
            unknown = [
                d for d in client.decisions if d.state == "unknown" and d.scenario_id == OBSTACLE
            ]
            assert unknown[-1].reason_codes == ["input_stale"]
            assert client.outputs.count("notice") == 2  # hud + speaker, one notice
            assert client.states.get(OBSTACLE) == "unknown"
        finally:
            await client.stop()

    run(scenario)


# --- UC-04: obstacle cleared -> resolved -> clear, alert played once, no notice ---


def test_uc04_cleared_obstacle_resolves_without_notice(server: LiveServer) -> None:
    async def scenario() -> None:
        client = await started(server)
        try:
            await client.play(fixture("uc04_obstacle_cleared"))
            await until(lambda: ("resolved", "clear") in transitions(client, OBSTACLE), 3)
            assert ("warning", "resolved") in transitions(client, OBSTACLE)
            assert len(alert_ids(client)) == 1
            assert client.outputs.count("ack_played") == 1
            assert client.outputs.count("notice") == 0
        finally:
            await client.stop()

    run(scenario)


# --- UC-05 / T-R10 / COM-04: reconnect re-checks, a re-sent alert is not replayed ---


def test_uc05_reconnect_resends_only_valid_alert_and_device_plays_once(
    server: LiveServer,
) -> None:
    async def scenario() -> None:
        client = await started(server)
        try:
            await client.play(fixture("uc01_obstacle")[:4])
            await until(lambda: client.outputs.count("ack_played") == 1)
            [alert_id] = alert_ids(client)
            session_id = client.session_id

            # 1) Normal reconnect: cursor 1 -> nothing re-sent, nothing replayed.
            await client.drop_connection()
            await until(lambda: len(client.sync_sent) == 2)
            assert client.sync_sent[-1] == (session_id, 1)  # same session (refresh)
            beats = client.outputs.count("ack_played")
            await asyncio.sleep(0.3)
            assert client.outputs.count("ack_played") == beats == 1

            # 2) Device lost its cursor but kept played IDs: server re-sends the same
            #    alert (still warning, latest, valid); device only ACKs played.
            client.last_alert_seq = 0
            await client.drop_connection()
            await until(lambda: client.outputs.count("ack_played") == 2)
            assert client.sync_sent[-1] == (session_id, 0)
            assert alert_ids(client) == [alert_id]
            assert client.outputs.count("alert", alert_id) == 2  # hud + speaker, first time only
            assert client.outputs.count("ack_received", alert_id) == 1
        finally:
            await client.stop()

    run(scenario)


# --- UC-08 / T-R16 / DEV-04: server stops -> one built-in notice -> reconnect -> alert_sync ---


def test_uc08_server_stop_announces_once_then_reconnects(server: LiveServer) -> None:
    async def scenario() -> None:
        client = await started(server)
        try:
            await client.play(fixture("uc01_obstacle")[:4])
            await until(lambda: client.outputs.count("ack_played") == 1)
            first_session = client.session_id

            await asyncio.to_thread(server.stop)
            stopped = time.monotonic()
            await until(lambda: client.outputs.count("disconnect") > 0, 3)
            assert time.monotonic() - stopped <= WATCHDOG_MS / 1000 + 0.5
            await asyncio.sleep(0.6)  # backoff retries continue without repeating it
            assert client.outputs.count("disconnect") == 2  # hud + speaker, once

            await asyncio.to_thread(server.start)
            await until(lambda: client.outputs.count("connection_restored") == 1, 5)
            # Restarted server lost the session: refresh 401 -> new session -> alert_sync(0).
            assert client.session_id != first_session
            assert client.sessions_created == 2
            assert client.sync_sent[-1] == (client.session_id, 0)
            assert client.outputs.count("disconnect") == 2
            assert [e.output for e in client.outputs.events if e.kind == "connection_restored"] == [
                "hud"
            ]
        finally:
            await client.stop()

    run(scenario)


# --- UC-09 / DEV-05: camera error reported as health down -> unknown(sensor_down) + notice ---


def test_uc09_camera_down_health_makes_unknown_with_notice(server: LiveServer) -> None:
    async def scenario() -> None:
        client = await started(server)
        try:
            # The fixture's frames, then the device's own health report (DEV-05).
            frames = [r for r in fixture("uc09_camera_down") if r.draft and r.draft.stream_id]
            await client.play([r for r in frames if r.draft and r.draft.payload.kind != "health"])
            await until(lambda: client.outputs.count("ack_played") == 1)
            assert await client.report_health("camera_0", "down", "camera_error")
            await until(lambda: ("warning", "unknown") in transitions(client, OBSTACLE), 3)
            last = transitions(client, OBSTACLE)[-1]
            assert last == ("warning", "unknown")
            unknown = [d for d in client.decisions if d.state == "unknown"][-1]
            assert unknown.reason_codes == ["sensor_down"]
            await until(lambda: client.outputs.count("notice") == 2)
        finally:
            await client.stop()

    run(scenario)


# --- session refusal and CLI ---


def test_wrong_secret_is_refused_without_retry(server: LiveServer) -> None:
    async def scenario() -> None:
        client = GlassClient(
            ClientConfig(server.url, "dev_example", "wrong", ("camera_0",), ("hud",))
        )
        with pytest.raises(SessionRejected) as info:
            await client.start()
        assert (info.value.status, info.value.code) == (401, "unauthorized")

    run(scenario)


def test_cli_plays_fixture_and_prints_id_only_output_log(
    server: LiveServer, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOA_SIM_SECRET", SECRET)
    code = sim_main(
        [
            "--fixture",
            str(SCENARIOS / "uc01_obstacle.jsonl"),
            "--server",
            server.url,
            "--device",
            "dev_example",
            "--outputs",
            "hud,speaker",
            "--linger-ms",
            "300",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert '"kind": "alert", "output": "hud"' in out
    assert '"kind": "ack_played"' in out
    assert "장애물" not in out and SECRET not in out


def test_cli_session_refused_exits_1(
    server: LiveServer, capsys: pytest.CaptureFixture[str]
) -> None:
    code = sim_main(
        [
            "--fixture",
            str(SCENARIOS / "uc01_obstacle.jsonl"),
            "--server",
            server.url,
            "--device",
            "dev_example",
            "--secret",
            "wrong",
        ]
    )
    assert code == 1
    assert "wrong" not in capsys.readouterr().err
