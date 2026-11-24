"""TASK-31 simulator real-model mode: frames -> provider -> server (ADR-0007; INP-01, COM-02).

The provider is RtDetrProvider with a fake detector and the frames are synthetic
byte files with a fake decoder, so this runs offline without weights. The server
runs the C0 profile so the `rtdetr-r18-coco` observations are its evidence.
"""

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration.live_server import LiveServer, make_workdir
from voa.providers.rtdetr import RawDetection, RtDetrProvider
from voa.simulator.__main__ import build_parser
from voa.simulator.__main__ import main as sim_main
from voa.simulator.adapter import ClientConfig, FramePlayStats, GlassClient
from voa.simulator.frames import FrameSource

PERSON_BOX = (200.0, 200.0, 440.0, 470.0)  # inside the C0 walkway ROI on 640x480
SCENARIO = "person_in_walkway"


def decode(data: bytes) -> list[RawDetection]:
    return [RawDetection("person", 0.9, PERSON_BOX)] if data == b"person" else []


class Detector:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s

    def detect(self, image: Any) -> tuple[int, int, list[RawDetection]]:
        if self.delay_s:
            time.sleep(self.delay_s)  # blocking, like real CPU inference
        return 640, 480, list(image)


def provider(delay_s: float = 0.0) -> RtDetrProvider:
    return RtDetrProvider(
        Detector(delay_s), model_id="rtdetr-r18-coco", revision="r", labels=["person"]
    )


def frames(tmp_path: Path, contents: list[bytes], fps: float) -> FrameSource:
    folder = tmp_path / "frames"
    folder.mkdir()
    for index, data in enumerate(contents):
        (folder / f"{index:05d}.jpg").write_bytes(data)
    return FrameSource(folder, fps=fps, decode=decode)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    workdir = make_workdir(tmp_path / "server")
    profile = workdir / "config" / "profiles" / "local_verify_c0.yaml"
    live = LiveServer(workdir, {"VOA_POLICY_PROFILE": str(profile)})
    live.start()
    yield live
    live.stop()


async def connected(live: LiveServer) -> GlassClient:
    client = GlassClient(
        ClientConfig(live.url, "dev_fixture", None, ("camera_0",), ("hud", "speaker"))
    )
    await client.start()
    await asyncio.wait_for(client.connected.wait(), 5)
    return client


async def until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def test_person_frames_raise_a_warning_alert_that_is_played(
    server: LiveServer, tmp_path: Path
) -> None:
    source = frames(tmp_path, [b"person"] * 6, fps=5)

    async def scenario() -> FramePlayStats:
        client = await connected(server)
        try:
            stats = await client.play_frames(source, provider())
            await until(lambda: client.outputs.count("ack_played") == 1)
            transitions = [
                (d.previous_state, d.state) for d in client.decisions if d.scenario_id == SCENARIO
            ]
            assert ("candidate", "warning") in transitions
            assert client.outputs.count("alert") == 2  # hud + speaker, once
            return stats
        finally:
            await client.stop()

    stats = asyncio.run(scenario())
    assert (stats.sent, stats.late_skipped, stats.total) == (6, 0, 6)


def test_slow_inference_skips_late_frames_without_blocking_the_link(
    server: LiveServer, tmp_path: Path
) -> None:
    """250 ms inference on a 10 fps folder: late frames are skipped and counted; the
    event loop keeps receiving heartbeats, so the 300 ms watchdog never fires."""
    source = frames(tmp_path, [b"empty"] * 12, fps=10)

    async def scenario() -> tuple[FramePlayStats, int]:
        client = await connected(server)
        try:
            stats = await client.play_frames(source, provider(delay_s=0.25))
            return stats, client.outputs.count("disconnect")
        finally:
            await client.stop()

    stats, disconnects = asyncio.run(scenario())
    assert stats.late_skipped > 0
    assert stats.sent + stats.late_skipped == stats.total == 12
    assert stats.provider_ms_p50 is not None and stats.provider_ms_p50 >= 250
    assert disconnects == 0


def test_offline_frames_are_counted_not_buffered(tmp_path: Path) -> None:
    """No connection: frames are processed but not sent, and nothing is queued for later."""
    source = frames(tmp_path, [b"person"] * 3, fps=20)
    client = GlassClient(ClientConfig("http://127.0.0.1:1", "d", None, ("camera_0",), ("hud",)))
    stats = asyncio.run(client.play_frames(source, provider()))
    assert (stats.sent, stats.offline_dropped, stats.total) == (0, 3, 3)


# --- CLI ---


def test_cli_needs_exactly_one_input_mode() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--server", "http://x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--fixture", "a.jsonl", "--frames", "dir", "--fps", "5"])
    args = parser.parse_args(["--frames", "dir", "--fps", "5"])
    assert args.model == "rtdetr-r18-coco"


def test_cli_frames_without_weights_or_vision_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_dir = tmp_path / "frames"
    source_dir.mkdir()
    (source_dir / "00000.jpg").write_bytes(b"person")
    code = sim_main(
        [
            "--frames",
            str(source_dir),
            "--fps",
            "5",
            "--weights-dir",
            str(tmp_path / "no-weights"),
        ]
    )
    err = capsys.readouterr().err
    assert code == 2 and "weights" in err and str(tmp_path) not in err


def test_cli_frames_requires_fps(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert sim_main(["--frames", str(tmp_path)]) == 2
