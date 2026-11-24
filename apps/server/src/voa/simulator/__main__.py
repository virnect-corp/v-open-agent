"""`python -m voa.simulator`: play a fixture as a glass client against a running server.

    python -m voa.simulator --fixture contracts/fixtures/scenarios/uc01_obstacle.jsonl \
        --server http://127.0.0.1:8765 --device dev_fixture --outputs hud,speaker

Stage C0 (ADR-0007): `--frames DIR --fps N [--model rtdetr-r18-coco]` runs the detector
here on each image frame (worker thread, late frames skipped) and sends the Drafts;
frame counters and provider latency go to stderr. Needs the `vision` group and local
weights (`scripts/vision.sh`); the server must run a profile that requires that model.

The virtual output log is printed as JSONL on stdout (IDs only, no alert text).
`--secret` may be omitted only for the insecure dev device; `VOA_SIM_SECRET` is
read when the flag is absent, so the secret need not appear in the process list.
Exit codes: 0 done, 1 session refused, 2 bad arguments, fixture, frames, weights or vision.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import cast, get_args

from voa.config.model_registry import load_manifests
from voa.providers.rtdetr import (
    DEFAULT_SCORE_FLOOR,
    VisionUnavailable,
    WeightsError,
    load_rtdetr,
)
from voa.simulator.adapter import ClientConfig, FramePlayStats, GlassClient, SessionRejected
from voa.simulator.fixture import FixtureError, load_drafts
from voa.simulator.frames import FramesError, FrameSource
from voa.simulator.outputs import OutputName

INPUT_NAMES = ("camera_0", "mic_0", "imu_0", "ptt_0")  # SPEC 7.4 InputCap
SERVER_ROOT = Path(__file__).resolve().parents[3]  # apps/server


def _names(value: str, allowed: Sequence[str], what: str) -> tuple[str, ...]:
    names = tuple(part.strip() for part in value.split(",") if part.strip())
    if not names or any(name not in allowed for name in names) or len(set(names)) != len(names):
        raise argparse.ArgumentTypeError(f"{what} must be a comma list of: {', '.join(allowed)}")
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m voa.simulator")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", help="JSONL fixture of observation Drafts")
    mode.add_argument("--frames", help="Stage C0: image frame folder run through a real model")
    parser.add_argument("--fps", type=float, default=None, help="frame rate of --frames")
    parser.add_argument("--model", default="rtdetr-r18-coco", help="detector manifest id")
    parser.add_argument("--models-dir", default=str(SERVER_ROOT / "config" / "models"))
    parser.add_argument(
        "--weights-dir",
        default=os.environ.get("VOA_MODEL_WEIGHTS_DIR", str(SERVER_ROOT / "var" / "models")),
    )
    parser.add_argument("--score-floor", type=float, default=DEFAULT_SCORE_FLOOR)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--device", default="dev_fixture")
    parser.add_argument("--secret", default=None)
    parser.add_argument(
        "--outputs",
        default="hud,speaker",
        type=lambda v: _names(v, get_args(OutputName), "--outputs"),
    )
    parser.add_argument(
        "--inputs", default="camera_0", type=lambda v: _names(v, INPUT_NAMES, "--inputs")
    )
    parser.add_argument(
        "--linger-ms", type=int, default=2000, help="keep listening after the last line"
    )
    return parser


Player = Callable[[GlassClient], Awaitable[object]]


async def run(config: ClientConfig, player: Player, linger_ms: int) -> int:
    client = GlassClient(config)
    try:
        await client.start()
    except SessionRejected as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("error: server unreachable", file=sys.stderr)
        return 1
    try:
        try:
            await asyncio.wait_for(client.connected.wait(), timeout=10)
        except TimeoutError:
            print("error: stream connection failed", file=sys.stderr)
            return 1
        result = await player(client)
        await asyncio.sleep(max(linger_ms, 0) / 1000)
    finally:
        await client.stop()
    for event in client.outputs.events:
        print(json.dumps(event.as_dict(), ensure_ascii=False))
    if isinstance(result, FramePlayStats):
        print(f"frames: {json.dumps(asdict(result))}", file=sys.stderr)
    return 0


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _frames_player(args: argparse.Namespace) -> Player | int:
    """Stage C0 (ADR-0007): frame folder -> detector on this side -> observation messages."""
    if args.fps is None:
        return _fail("--frames needs --fps")
    try:
        source = FrameSource(args.frames, args.fps)
    except FramesError as error:
        return _fail(str(error))
    manifests = load_manifests(args.models_dir)
    if not isinstance(manifests, dict) or args.model not in manifests:
        return _fail("model manifest not found")
    try:
        provider = load_rtdetr(
            manifests[args.model], Path(args.weights_dir), score_floor=args.score_floor
        )
    except (WeightsError, VisionUnavailable, ValueError) as error:
        return _fail(str(error))
    return lambda client: client.play_frames(source, provider)


def _fixture_player(args: argparse.Namespace) -> Player | int:
    try:
        rows = load_drafts(args.fixture)
    except FixtureError as error:
        return _fail(str(error))
    except OSError:
        return _fail("fixture not readable")
    return lambda client: client.play(rows)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    player = _frames_player(args) if args.frames is not None else _fixture_player(args)
    if isinstance(player, int):
        return player
    config = ClientConfig(
        server_url=args.server,
        device_id=args.device,
        secret=args.secret if args.secret is not None else os.environ.get("VOA_SIM_SECRET"),
        inputs=args.inputs,
        outputs=cast(tuple[OutputName, ...], args.outputs),
    )
    return asyncio.run(run(config, player, args.linger_ms))


if __name__ == "__main__":
    sys.exit(main())
