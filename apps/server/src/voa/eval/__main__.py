"""`python -m voa.eval`: evaluate a frame folder with the real detector (ADR-0007).

    scripts/vision.sh python -m voa.eval --frames apps/server/var/frames/walk01 --fps 5

Decisions are printed as JSONL on stdout; the report (PLAN section 4 format) goes
to stderr, or to `--report PATH`. The detector model is the single
`object_detection` model the profile requires. Weights are read from
`--weights-dir` (default `VOA_MODEL_WEIGHTS_DIR` or apps/server/var/models) and
checked against the manifest sha256; nothing is downloaded.
Exit codes: 0 done, 2 bad arguments, frames, profile, weights or missing `vision`.
Error messages never contain host paths.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from voa.config.model_registry import check_profile_models, load_manifests
from voa.config.profile_loader import load_profile
from voa.engine.profile import PolicyProfile, ProfileError
from voa.eval.runner import run_eval
from voa.providers.rtdetr import (
    DEFAULT_SCORE_FLOOR,
    VisionUnavailable,
    WeightsError,
    load_rtdetr,
)
from voa.simulator.frames import FramesError, FrameSource

SERVER_ROOT = Path(__file__).resolve().parents[3]  # apps/server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m voa.eval")
    parser.add_argument("--frames", required=True, help="folder of .jpg/.jpeg/.png frames")
    parser.add_argument("--fps", required=True, type=float, help="frame rate of the folder")
    parser.add_argument(
        "--profile", default=str(SERVER_ROOT / "config" / "profiles" / "local_verify_c0.yaml")
    )
    parser.add_argument("--models-dir", default=str(SERVER_ROOT / "config" / "models"))
    parser.add_argument(
        "--weights-dir",
        default=os.environ.get("VOA_MODEL_WEIGHTS_DIR", str(SERVER_ROOT / "var" / "models")),
    )
    parser.add_argument("--score-floor", type=float, default=DEFAULT_SCORE_FLOOR)
    parser.add_argument(
        "--tick-ms", type=int, default=100, help="engine tick interval (server default 100)"
    )
    parser.add_argument("--report", default=None, help="write the JSON report here")
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--code-commit", default="TBD")
    return parser


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.tick_ms <= 0:
        return _fail("tick-ms must be positive")
    try:
        source = FrameSource(args.frames, args.fps)
    except FramesError as error:
        return _fail(str(error))

    profile = load_profile(args.profile)
    if isinstance(profile, ProfileError):
        return _fail(f"profile: {profile.code}")
    manifests = load_manifests(args.models_dir)
    if isinstance(manifests, ProfileError):
        return _fail(f"model manifests: {manifests.code}")
    mismatch = check_profile_models(profile, manifests)
    if mismatch is not None:
        return _fail(f"profile and manifests: {mismatch.code}")
    model_ids = _detector_models(profile)
    if len(model_ids) != 1:
        return _fail("profile must require exactly one object_detection model")
    manifest = manifests[model_ids[0]]

    try:
        provider = load_rtdetr(manifest, Path(args.weights_dir), score_floor=args.score_floor)
    except (WeightsError, VisionUnavailable, ValueError) as error:
        return _fail(str(error))

    result = run_eval(
        source,
        provider,
        profile,
        sha256=manifest.sha256,
        run_id=args.run_id,
        code_commit=args.code_commit,
        tick_ms=args.tick_ms,
    )
    for decision in result.decisions:
        print(decision.model_dump_json())
    report = json.dumps(result.report.as_dict(), ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(report + "\n", encoding="utf-8")
    else:
        print(report, file=sys.stderr)
    return 0


def _detector_models(profile: PolicyProfile) -> list[str]:
    return sorted(
        {
            scenario.required_models["object_detection"]
            for scenario in profile.scenarios
            if "object_detection" in scenario.required_models
        }
    )


if __name__ == "__main__":
    sys.exit(main())
