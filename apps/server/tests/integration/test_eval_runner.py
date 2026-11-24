"""TASK-30 frame folder input and offline evaluation runner (ADR-0007; NFR-02, NFR-03, T-R14).

Frames are synthetic byte files decoded by a fake decoder, and the detector is a
fake that reads the "image" it is handed, so no real image or weights are needed.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from voa.config.profile_loader import load_profile
from voa.engine.profile import PolicyProfile
from voa.eval.__main__ import main as eval_main
from voa.eval.runner import EvalReport, run_eval
from voa.providers.rtdetr import RawDetection, RtDetrProvider
from voa.simulator.frames import FramesError, FrameSource

SERVER = Path(__file__).resolve().parents[2]
PROFILE = SERVER / "config" / "profiles" / "local_verify_c0.yaml"
SCENARIO = "person_in_walkway"
PERSON_BOX = (200.0, 200.0, 440.0, 470.0)  # inside the C0 walkway ROI on 640x480


def decode(data: bytes) -> list[RawDetection]:
    """Fake decoder: b"person" -> one person box, b"empty" -> nothing, else corrupt."""
    if data == b"person":
        return [RawDetection("person", 0.9, PERSON_BOX)]
    if data == b"empty":
        return []
    raise ValueError("corrupt")


class ListDetector:
    def detect(self, image: Any) -> tuple[int, int, list[RawDetection]]:
        return 640, 480, list(image)


def provider() -> RtDetrProvider:
    return RtDetrProvider(
        ListDetector(), model_id="rtdetr-r18-coco", revision="r", labels=["person"]
    )


def profile() -> PolicyProfile:
    loaded = load_profile(PROFILE)
    assert isinstance(loaded, PolicyProfile)
    return loaded


def frames_dir(tmp_path: Path, contents: list[bytes], suffix: str = ".jpg") -> Path:
    folder = tmp_path / "walk01"
    folder.mkdir()
    for index, data in enumerate(contents):
        (folder / f"{index:05d}{suffix}").write_bytes(data)
    return folder


def snapshot(root: Path) -> list[tuple[str, int]]:
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*"))


class StepTimer:
    """Deterministic timer: every call advances 1 ms."""

    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        self.now += 1_000_000
        return self.now


# --- frame source ---


def test_frames_are_read_in_name_order_with_fps_timestamps(tmp_path: Path) -> None:
    folder = tmp_path / "f"
    folder.mkdir()
    for name in ("00003.png", "00001.jpg", "00002.jpeg", "notes.txt", ".hidden.jpg"):
        (folder / name).write_bytes(b"empty")
    source = FrameSource(folder, fps=5, decode=decode)
    frames = list(source)
    assert [f.frame_id for f in frames] == ["frame_00000", "frame_00001", "frame_00002"]
    assert [f.capture_ts_monotonic_ns for f in frames] == [0, 200_000_000, 400_000_000]
    assert source.skipped == 0 and len(source) == 3


def test_corrupt_frames_are_skipped_and_counted_but_keep_their_slot(tmp_path: Path) -> None:
    folder = frames_dir(tmp_path, [b"person", b"\x00broken", b"empty"])
    source = FrameSource(folder, fps=10, decode=decode)
    frames = list(source)
    assert [f.frame_id for f in frames] == ["frame_00000", "frame_00002"]
    assert [f.capture_ts_monotonic_ns for f in frames] == [0, 200_000_000]  # time keeps going
    assert source.skipped == 1


def test_empty_missing_or_bad_fps_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(FramesError, match="no image"):
        FrameSource(tmp_path / "empty", fps=5, decode=decode)
    with pytest.raises(FramesError, match="not a folder"):
        FrameSource(tmp_path / "missing", fps=5, decode=decode)
    folder = frames_dir(tmp_path, [b"empty"])
    with pytest.raises(FramesError, match="fps"):
        FrameSource(folder, fps=0, decode=decode)


def test_reading_frames_creates_and_changes_nothing(tmp_path: Path) -> None:
    folder = frames_dir(tmp_path, [b"person", b"empty"])
    before = snapshot(tmp_path)
    list(FrameSource(folder, fps=5, decode=decode))
    assert snapshot(tmp_path) == before


# --- evaluation runner ---


def run(folder: Path, fps: float = 5) -> tuple[list[str], EvalReport]:
    source = FrameSource(folder, fps=fps, decode=decode)
    result = run_eval(source, provider(), profile(), timer=StepTimer())
    return [d.model_dump_json() for d in result.decisions], result.report


def test_person_frames_reach_warning_with_one_alert(tmp_path: Path) -> None:
    folder = frames_dir(tmp_path, [b"person"] * 4)
    decisions, report = run(folder)
    states = [(json.loads(d)["previous_state"], json.loads(d)["state"]) for d in decisions]
    assert ("candidate", "warning") in states
    assert report.measurements["alerts"] == 1
    assert report.measurements["frames"] == 4


def test_decisions_are_byte_identical_across_runs(tmp_path: Path) -> None:
    folder = frames_dir(tmp_path, [b"person"] * 3 + [b"empty"] * 4)
    assert run(folder)[0] == run(folder)[0]


def test_report_measures_latency_and_leaves_unmeasured_metrics_tbd(tmp_path: Path) -> None:
    folder = frames_dir(tmp_path, [b"person", b"\x00", b"empty"])
    _, report = run(folder)
    body: dict[str, Any] = json.loads(json.dumps(report.as_dict()))
    assert body["metrics"] == {
        "event_recall": "TBD",
        "false_alerts_per_hour": "TBD",
        "e2e_p95_ms": "TBD",
        "unknown_rate": body["metrics"]["unknown_rate"],
        "unsupported_claim_rate": "TBD",
    }
    assert 0.0 <= body["metrics"]["unknown_rate"] <= 1.0
    m = body["measurements"]
    # StepTimer advances 1 ms per call: each timed section measures exactly 1 ms.
    assert m["provider_ms"] == {"n": 2, "p50": 1.0, "p95": 1.0}
    assert m["step_us"] == {"n": 2, "p50": 1000, "p95": 1000}
    assert (m["frames"], m["skipped_frames"]) == (2, 1)
    assert body["dataset"] == {"name": "walk01", "frames": 3, "fps": 5}
    assert body["model_artifacts"] == [{"id": "rtdetr-r18-coco", "revision": "r", "sha256": "TBD"}]
    assert str(tmp_path) not in json.dumps(body)  # folder name only, no host paths


def test_input_gap_longer_than_staleness_turns_unknown(tmp_path: Path) -> None:
    """At 0.5 fps frames are 2 s apart (> unknown_after_ms 1.5 s): never a warning."""
    folder = frames_dir(tmp_path, [b"person"] * 4)
    decisions, report = run(folder, fps=0.5)
    states = [json.loads(d)["state"] for d in decisions]
    assert "warning" not in states
    assert "unknown" in states[1:]  # ticks between frames detect the gap
    assert report.measurements["alerts"] == 0


def test_ticks_between_frames_do_not_disturb_a_normal_rate(tmp_path: Path) -> None:
    """At 5 fps (200 ms) ticks run between frames and the warning still comes once."""
    folder = frames_dir(tmp_path, [b"person"] * 6)
    decisions, report = run(folder)
    assert [json.loads(d)["state"] for d in decisions].count("warning") == 1
    assert report.measurements["alerts"] == 1


# --- CLI ---


@pytest.fixture
def no_vision(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".")[0] in {"torch", "transformers", "PIL"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    yield


def test_cli_without_weights_or_vision_exits_2_without_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_vision: None
) -> None:
    folder = frames_dir(tmp_path, [b"person"])
    code = eval_main(
        ["--frames", str(folder), "--fps", "5", "--weights-dir", str(tmp_path / "weights")]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "weights" in err and str(tmp_path) not in err


def test_cli_rejects_bad_fps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = frames_dir(tmp_path, [b"person"])
    assert eval_main(["--frames", str(folder), "--fps", "-1"]) == 2


def test_pillow_decode_reads_rgb_and_rejects_garbage(tmp_path: Path) -> None:
    """vision image only: an in-memory PNG decodes, garbage is skipped as corrupt."""
    image_module = pytest.importorskip("PIL.Image")
    import io

    from voa.simulator.frames import pillow_decode

    buffer = io.BytesIO()
    image_module.new("L", (8, 6)).save(buffer, format="PNG")
    decoded = pillow_decode(buffer.getvalue())
    assert decoded.mode == "RGB" and decoded.size == (8, 6)
    folder = frames_dir(tmp_path, [buffer.getvalue(), b"not an image"], suffix=".png")
    source = FrameSource(folder, fps=5)
    assert [f.frame_id for f in source] == ["frame_00000"] and source.skipped == 1
