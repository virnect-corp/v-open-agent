"""TASK-13 replay runner and CLI (SPEC 5; INP-02, ENG-14, NFR-02, INV-05)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from voa.contracts.decision import RiskDecision
from voa.replay.__main__ import main
from voa.replay.runner import first_mismatch, percentile, run_events
from voa.simulator.fixture import decision_view, load_expected

REPO = Path(__file__).resolve().parents[4]
SCENARIOS = REPO / "contracts" / "fixtures" / "scenarios"
UC = ["uc01_obstacle", "uc02_moving_person", "uc03_input_lost", "uc04_obstacle_cleared"]


def fixture(name: str) -> str:
    return str(SCENARIOS / f"{name}.jsonl")


def expected(name: str) -> str:
    return str(SCENARIOS / f"{name}.expected.jsonl")


def run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


# --- Success ---


@pytest.mark.parametrize("name", UC)
def test_matching_expected_exits_zero_and_prints_decisions(
    capsys: pytest.CaptureFixture[str], name: str
) -> None:
    code, out, err = run_cli(capsys, fixture(name), "--expected", expected(name))
    assert code == 0, err
    decisions = [RiskDecision.model_validate_json(row) for row in out.splitlines()]
    assert [decision_view(d) for d in decisions] == load_expected(expected(name))
    assert "mismatch" not in err


@pytest.mark.parametrize("name", UC)
def test_two_runs_print_byte_identical_output(
    capsys: pytest.CaptureFixture[str], name: str
) -> None:
    first = run_cli(capsys, fixture(name))[1]
    second = run_cli(capsys, fixture(name))[1]
    assert first == second and first.endswith("\n")


def test_timing_goes_to_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run_cli(capsys, fixture("uc01_obstacle"))
    assert code == 0
    assert "p50" not in out and "p95" not in out
    timing = err.strip().splitlines()[-1]
    assert timing.startswith("step_us ")
    fields = dict(part.split("=") for part in timing.split()[1:])
    assert fields["n"] == "4"  # four observations, the tick is not a step
    assert int(fields["p50"]) >= 0 and int(fields["p95"]) >= int(fields["p50"])


def test_gate_command_runs_as_a_module() -> None:
    """Stage A gate: `python -m voa.replay uc01_obstacle.jsonl --expected ...` exits 0."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voa.replay",
            fixture("uc01_obstacle"),
            "--expected",
            expected("uc01_obstacle"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONHASHSEED": "0"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 2


# --- Failure: mismatches report the first differing line and field ---


def write_expected(tmp_path: Path, rows: list[dict[str, object]]) -> str:
    path = tmp_path / "expected.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return str(path)


def test_field_mismatch_reports_line_and_field(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rows = load_expected(expected("uc01_obstacle"))
    rows[1] = {**rows[1], "alert_id": "alert_0009"}
    code, out, err = run_cli(
        capsys, fixture("uc01_obstacle"), "--expected", write_expected(tmp_path, rows)
    )
    assert code == 1
    assert "mismatch at expected line 2: field alert_id" in err
    assert "alert_0009" in err and "alert_0001" in err
    assert len(out.splitlines()) == 2  # decisions are still printed


def test_missing_and_extra_decisions_are_reported(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rows = load_expected(expected("uc01_obstacle"))
    code, _, err = run_cli(
        capsys, fixture("uc01_obstacle"), "--expected", write_expected(tmp_path, rows[:1])
    )
    assert code == 1 and "line 2: unexpected extra decision" in err

    code, _, err = run_cli(
        capsys,
        fixture("uc01_obstacle"),
        "--expected",
        write_expected(tmp_path, [*rows, rows[-1]]),
    )
    assert code == 1 and "line 3: expected decision missing" in err


def test_first_mismatch_checks_fields_in_spec_order() -> None:
    from tests.integration.test_fixture_replay import replay

    decisions = replay("uc04_obstacle_cleared")
    rows = load_expected(expected("uc04_obstacle_cleared"))
    assert first_mismatch(decisions, rows) is None
    rows[2] = {**rows[2], "state": "clear", "evidence_ids": []}
    mismatch = first_mismatch(decisions, rows)
    assert mismatch is not None
    assert (mismatch.line_no, mismatch.field) == (3, "state")
    assert (mismatch.expected, mismatch.actual) == ("clear", "resolved")


# --- Failure: bad inputs exit 2 with a readable message, no traceback ---


def test_bad_fixture_line_exits_two(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"at_ms": 5, "tick": true}\n{"at_ms": 1, "tick": true}\n', encoding="utf-8")
    code, out, err = run_cli(capsys, str(bad))
    assert code == 2 and out == ""
    assert "line 2" in err and "Traceback" not in err


def test_missing_files_exit_two(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _, err = run_cli(capsys, str(tmp_path / "nope.jsonl"))
    assert code == 2 and "not found" in err
    code, _, err = run_cli(
        capsys, fixture("uc01_obstacle"), "--expected", str(tmp_path / "nope.jsonl")
    )
    assert code == 2 and "not found" in err


def test_invalid_profile_exits_two_instead_of_silent_pass(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    profile = tmp_path / "bad.yaml"
    profile.write_text("profile_id: x\n", encoding="utf-8")
    code, out, err = run_cli(
        capsys,
        fixture("uc01_obstacle"),
        "--profile",
        str(profile),
        "--expected",
        expected("uc01_obstacle"),
    )
    assert code == 2 and out == ""
    assert "profile_invalid" in err


# --- Missing input: an empty fixture yields no decisions and no timing samples ---


def test_empty_fixture(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    code, out, err = run_cli(capsys, str(empty))
    assert code == 0 and out == ""
    assert err.strip().splitlines()[-1] == "step_us n=0 p50=- p95=-"


# --- Runner internals ---


def test_runner_times_only_steps_and_never_changes_decisions() -> None:
    from voa.config.profile_loader import load_profile
    from voa.engine.ports import FixedEpochClock
    from voa.engine.profile import PolicyProfile
    from voa.simulator.fixture import load_fixture

    profile = load_profile(REPO / "apps/server/config/profiles/indoor_research_v0.yaml")
    assert isinstance(profile, PolicyProfile)
    events = load_fixture(fixture("uc03_input_lost"), FixedEpochClock())
    ticks = iter(range(0, 10_000_000, 1_000))  # fake timer: every reading +1 us
    result = run_events(events, profile, timer=lambda: next(ticks))
    assert result.step_ns == [1_000, 1_000, 1_000]  # 3 observations; 3 ticks untimed
    baseline = run_events(events, profile)
    assert [d.model_dump_json() for d in result.decisions] == [
        d.model_dump_json() for d in baseline.decisions
    ]


@pytest.mark.parametrize(
    ("values", "q", "result"),
    [
        (list(range(1, 101)), 50, 50),
        (list(range(1, 101)), 95, 95),
        ([7], 50, 7),
        ([7], 95, 7),
        ([30, 10, 20], 50, 20),
        ([30, 10, 20], 95, 30),
    ],
)
def test_percentile_nearest_rank(values: list[int], q: int, result: int) -> None:
    assert percentile(values, q) == result
