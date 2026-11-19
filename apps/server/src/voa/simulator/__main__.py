"""`python -m voa.simulator`: play a fixture as a glass client against a running server.

    python -m voa.simulator --fixture contracts/fixtures/scenarios/uc01_obstacle.jsonl \
        --server http://127.0.0.1:8765 --device dev_fixture --outputs hud,speaker

The virtual output log is printed as JSONL on stdout (IDs only, no alert text).
`--secret` may be omitted only for the insecure dev device; `VOA_SIM_SECRET` is
read when the flag is absent, so the secret need not appear in the process list.
Exit codes: 0 done, 1 session refused, 2 bad arguments or fixture.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from typing import cast, get_args

from voa.simulator.adapter import ClientConfig, GlassClient, SessionRejected
from voa.simulator.fixture import FixtureError, TimedDraft, load_drafts
from voa.simulator.outputs import OutputName

INPUT_NAMES = ("camera_0", "mic_0", "imu_0", "ptt_0")  # SPEC 7.4 InputCap


def _names(value: str, allowed: Sequence[str], what: str) -> tuple[str, ...]:
    names = tuple(part.strip() for part in value.split(",") if part.strip())
    if not names or any(name not in allowed for name in names) or len(set(names)) != len(names):
        raise argparse.ArgumentTypeError(f"{what} must be a comma list of: {', '.join(allowed)}")
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m voa.simulator")
    parser.add_argument("--fixture", required=True)
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


async def run(config: ClientConfig, rows: list[TimedDraft], linger_ms: int) -> int:
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
        await client.play(rows)
        await asyncio.sleep(max(linger_ms, 0) / 1000)
    finally:
        await client.stop()
    for event in client.outputs.events:
        print(json.dumps(event.as_dict(), ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rows = load_drafts(args.fixture)
    except FixtureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError:
        print("error: fixture not readable", file=sys.stderr)
        return 2
    config = ClientConfig(
        server_url=args.server,
        device_id=args.device,
        secret=args.secret if args.secret is not None else os.environ.get("VOA_SIM_SECRET"),
        inputs=args.inputs,
        outputs=cast(tuple[OutputName, ...], args.outputs),
    )
    return asyncio.run(run(config, rows, args.linger_ms))


if __name__ == "__main__":
    sys.exit(main())
