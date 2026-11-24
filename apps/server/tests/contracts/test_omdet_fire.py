"""TASK-33 C1a open-vocabulary detector and fire profile (ADR-0008; INP-05, ENG-19).

The detector family is chosen from the local config.json after the sha256 check;
fake factories stand in for torch/transformers so this runs offline. The fire
profile is exercised through the evaluation runner with a fake detector.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from voa.config.model_registry import ModelManifest, check_profile_models, load_manifests
from voa.config.profile_loader import load_profile
from voa.engine.profile import PolicyProfile
from voa.eval.__main__ import main as eval_main
from voa.eval.runner import run_eval
from voa.providers.detectors import load_detector
from voa.providers.rtdetr import WEIGHTS_FILE, Frame, RawDetection, WeightsError
from voa.simulator.frames import FrameSource

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
MODEL_ID = "omdet-turbo-fire"
FIRE_BOX = (100.0, 80.0, 220.0, 260.0)
SMOKE_BOX = (300.0, 0.0, 600.0, 200.0)


def manifests() -> dict[str, ModelManifest]:
    loaded = load_manifests(CONFIG / "models")
    assert isinstance(loaded, dict)
    return loaded


def fire_profile() -> PolicyProfile:
    loaded = load_profile(CONFIG / "profiles" / "fire_c1.yaml")
    assert isinstance(loaded, PolicyProfile)
    return loaded


# --- manifest and profile ---


def test_manifest_pins_revision_hash_license_and_prompts() -> None:
    manifest = manifests()[MODEL_ID]
    assert manifest.kind == "object_detection"
    assert manifest.labels == ["fire", "smoke"]
    assert manifest.weights == "file" and manifest.sha256 is not None
    assert len(manifest.revision) == 40
    assert manifest.license == "Apache-2.0"


def test_fire_profile_matches_manifests_and_ignores_smoke() -> None:
    profile = fire_profile()
    [scenario] = profile.scenarios
    assert scenario.id == "fire_in_view" and scenario.labels == ["fire"]
    assert scenario.required_models == {"object_detection": MODEL_ID}
    assert scenario.roi == (0.0, 0.0, 1.0, 1.0)
    assert scenario.severity == "attention"
    assert check_profile_models(profile, manifests()) is None


# --- loader: sha256 first, then model_type from config.json ---


class FakeDetector:
    def __init__(self, labels: tuple[str, ...]) -> None:
        self.labels = labels

    def detect(self, image: Any) -> tuple[int, int, list[RawDetection]]:
        return 640, 480, list(image)


def weights(tmp_path: Path, model_type: str | None) -> tuple[Path, ModelManifest]:
    folder = tmp_path / MODEL_ID
    folder.mkdir()
    data = b"fake weights"
    (folder / WEIGHTS_FILE).write_bytes(data)
    if model_type is not None:
        (folder / "config.json").write_text(json.dumps({"model_type": model_type}))
    manifest = manifests()[MODEL_ID].model_copy(update={"sha256": hashlib.sha256(data).hexdigest()})
    return tmp_path, manifest


def test_loader_picks_the_family_and_prompts_with_manifest_labels(tmp_path: Path) -> None:
    root, manifest = weights(tmp_path, "omdet-turbo")
    seen: list[tuple[str, ...]] = []

    def omdet(folder: Path, labels: Any, floor: float) -> FakeDetector:
        seen.append(tuple(labels))
        return FakeDetector(tuple(labels))

    provider = load_detector(manifest, root, factories={"omdet-turbo": omdet})
    assert seen == [("fire", "smoke")]
    [draft] = provider.process(
        Frame("f0", 0, [RawDetection("fire", 0.6, FIRE_BOX), RawDetection("cat", 0.9, FIRE_BOX)])
    )
    assert draft.model is not None and draft.model.id == MODEL_ID
    assert [d.label for d in draft.payload.detections] == ["fire"]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("model_type", "message"),
    [(None, "config.json"), ("yolo", "unsupported model_type")],
)
def test_loader_rejects_missing_or_unknown_model_type(
    tmp_path: Path, model_type: str | None, message: str
) -> None:
    root, manifest = weights(tmp_path, model_type)
    with pytest.raises(WeightsError, match=message) as error:
        load_detector(manifest, root, factories={})
    assert str(tmp_path) not in str(error.value)


def test_loader_checks_sha256_before_reading_config(tmp_path: Path) -> None:
    root, manifest = weights(tmp_path, "omdet-turbo")
    bad = manifest.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(WeightsError, match="sha256"):
        load_detector(bad, root, factories={"omdet-turbo": lambda *a: FakeDetector(())})


# --- fire scenario through the evaluation runner ---


def decode(data: bytes) -> list[RawDetection]:
    return {
        b"fire": [RawDetection("fire", 0.55, FIRE_BOX)],
        b"smoke": [RawDetection("smoke", 0.8, SMOKE_BOX)],
        b"weak": [RawDetection("fire", 0.28, FIRE_BOX)],  # above provider floor, below min_score
        b"empty": [],
    }[data]


def run(tmp_path: Path, contents: list[bytes], fps: float = 5) -> list[tuple[str | None, str]]:
    folder = tmp_path / "clip"
    folder.mkdir()
    for index, data in enumerate(contents):
        (folder / f"{index:05d}.jpg").write_bytes(data)
    from voa.providers.rtdetr import RtDetrProvider

    provider = RtDetrProvider(
        FakeDetector(("fire", "smoke")), model_id=MODEL_ID, revision="r", labels=["fire", "smoke"]
    )
    result = run_eval(FrameSource(folder, fps=fps, decode=decode), provider, fire_profile())
    return [(d.previous_state, d.state) for d in result.decisions]


def test_flames_warn_once_then_resolve(tmp_path: Path) -> None:
    transitions = run(tmp_path, [b"empty"] * 3 + [b"fire"] * 6 + [b"empty"] * 5)
    assert transitions.count(("candidate", "warning")) == 1
    assert ("warning", "resolved") in transitions


def test_smoke_alone_or_weak_fire_never_warns(tmp_path: Path) -> None:
    transitions = run(tmp_path, [b"smoke"] * 6 + [b"weak"] * 6)
    assert all(state != "warning" for _, state in transitions)


def test_frame_gap_longer_than_staleness_is_unknown_not_warning(tmp_path: Path) -> None:
    transitions = run(tmp_path, [b"fire"] * 4, fps=0.5)
    states = [state for _, state in transitions]
    assert "warning" not in states and "unknown" in states


def test_eval_cli_with_fire_profile_and_no_weights_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "clip"
    folder.mkdir()
    (folder / "00000.jpg").write_bytes(b"fire")
    code = eval_main(
        [
            "--frames", str(folder), "--fps", "5",
            "--profile", str(CONFIG / "profiles" / "fire_c1.yaml"),
            "--weights-dir", str(tmp_path / "none"),
        ]
    )  # fmt: skip
    err = capsys.readouterr().err
    assert code == 2 and "omdet-turbo-fire" in err and str(tmp_path) not in err


# --- real weights (excluded by default) ---


@pytest.mark.model
def test_real_omdet_loads_and_emits_only_prompt_labels() -> None:
    from PIL import Image

    root = Path(os.environ.get("VOA_MODEL_WEIGHTS_DIR", SERVER / "var" / "models"))
    provider = load_detector(manifests()[MODEL_ID], root)
    image = Image.new("RGB", (640, 480), (200, 90, 20))  # in memory only
    [draft] = provider.process(Frame("f0", 0, image))
    assert draft.payload.kind == "object_detection"
    assert {d.label for d in draft.payload.detections} <= {"fire", "smoke"}
