"""`python -m voa.replay <fixture.jsonl> [--profile path] [--expected path]` (SPEC 5).

stdout: one RiskDecision JSON per line. stderr: diagnostics and step timing.
Exit codes: 0 = done (and matched, if --expected), 1 = mismatch, 2 = bad input.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from voa.config.profile_loader import load_profile
from voa.engine.ports import FixedEpochClock
from voa.engine.profile import ProfileError
from voa.replay.runner import first_mismatch, percentile, run_events
from voa.simulator.fixture import FixtureError, load_expected, load_fixture

DEFAULT_PROFILE = Path(__file__).resolve().parents[3] / "config/profiles/indoor_research_v0.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m voa.replay", description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--expected", type=Path)
    return parser


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    profile = load_profile(args.profile)
    if isinstance(profile, ProfileError):
        # An invalid profile decides nothing (INV-10); exiting 0 would look like a pass.
        return _fail(f"profile {profile.code}: {profile.detail}")

    try:
        events = load_fixture(args.fixture, FixedEpochClock())
        expected = load_expected(args.expected) if args.expected is not None else None
    except FileNotFoundError as error:
        return _fail(f"not found: {error.filename}")
    except FixtureError as error:
        return _fail(f"invalid fixture: {error}")
    except (OSError, UnicodeError):
        return _fail("cannot read input file")

    result = run_events(events, profile)
    sys.stdout.writelines(decision.model_dump_json() + "\n" for decision in result.decisions)
    sys.stdout.flush()

    code = 0
    if expected is not None:
        mismatch = first_mismatch(result.decisions, expected)
        if mismatch is not None:
            print(mismatch.describe(), file=sys.stderr)
            code = 1

    samples = [ns // 1_000 for ns in result.step_ns]
    if samples:
        timing = f"n={len(samples)} p50={percentile(samples, 50)} p95={percentile(samples, 95)}"
    else:
        timing = "n=0 p50=- p95=-"
    print(f"step_us {timing}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
