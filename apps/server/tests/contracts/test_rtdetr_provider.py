"""TASK-29 RT-DETR provider without weights (ADR-0007; INP-03, INP-05, CON-03).

A fake detector stands in for transformers/torch, so these run offline in the
default dev image. The real model runs only in `@pytest.mark.model` tests.
"""

import builtins
import hashlib
import math
import os
from pathlib import Path
from typing import Any

import pytest

from voa.config.model_registry import ModelManifest, check_profile_models, load_manifests
from voa.config.profile_loader import load_profile
from voa.contracts.observation import Detection, DetectionPayload, ObservationDraft
from voa.engine.profile import PolicyProfile
from voa.providers.rtdetr import (
    WEIGHTS_FILE,
    Frame,
    RawDetection,
    RtDetrProvider,
    VisionUnavailable,
    WeightsError,
    load_rtdetr,
    verify_weights,
)

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
MODEL_ID = "rtdetr-r18-coco"


class FakeDetector:
    """Returns canned pixel-space detections for a (width, height) "image"."""

    def __init__(self, detections: list[RawDetection]) -> None:
        self.detections = detections
        self.calls = 0

    def detect(self, image: Any) -> tuple[int, int, list[RawDetection]]:
        self.calls += 1
        width, height = image
        return width, height, list(self.detections)


def provider(detections: list[RawDetection], **kwargs: Any) -> RtDetrProvider:
    return RtDetrProvider(
        FakeDetector(detections),
        model_id=MODEL_ID,
        revision="abc123",
        labels=("person",),
        **kwargs,
    )


def dets(draft: ObservationDraft) -> list[Detection]:
    assert isinstance(draft.payload, DetectionPayload)
    return draft.payload.detections


def frame(i: int = 0, size: tuple[int, int] = (640, 480)) -> Frame:
    return Frame(frame_id=f"f{i}", capture_ts_monotonic_ns=i * 100_000_000, image=size)


# --- Draft shape (CON-03) ---


def test_person_box_is_normalized_and_carries_model_origin() -> None:
    p = provider([RawDetection("person", 0.9, (64.0, 48.0, 320.0, 480.0))])
    [draft] = p.process(frame())
    assert draft.stream_id == "camera_0" and draft.seq == 0
    assert draft.model is not None
    assert (draft.model.id, draft.model.revision) == (MODEL_ID, "abc123")
    assert draft.payload.kind == "object_detection"
    [det] = dets(draft)
    assert det.label == "person" and det.score == pytest.approx(0.9)
    assert det.bbox == pytest.approx((0.1, 0.1, 0.5, 1.0))


def test_labels_outside_the_manifest_and_low_scores_are_dropped() -> None:
    p = provider(
        [
            RawDetection("chair", 0.95, (0, 0, 10, 10)),
            RawDetection("person", 0.2, (0, 0, 10, 10)),  # below the default floor 0.25
            RawDetection("person", 0.25, (0, 0, 10, 10)),  # boundary: kept
        ]
    )
    [draft] = p.process(frame())
    assert [(d.label, d.score) for d in dets(draft)] == [("person", 0.25)]


def test_frame_with_nothing_seen_still_yields_a_draft() -> None:
    """An empty detection list is evidence of absence, not missing input."""
    [draft] = provider([]).process(frame())
    assert dets(draft) == []


def test_out_of_image_boxes_are_clamped_and_degenerate_boxes_dropped() -> None:
    p = provider(
        [
            RawDetection("person", 0.9, (-20.0, -5.0, 700.0, 500.0)),  # clamped to the frame
            RawDetection("person", 0.9, (100.0, 100.0, 100.0, 200.0)),  # zero width
            RawDetection("person", 0.9, (700.0, 10.0, 800.0, 20.0)),  # fully outside
            RawDetection("person", math.nan, (0, 0, 10, 10)),
        ]
    )
    [draft] = p.process(frame())
    assert [d.bbox for d in dets(draft)] == [(0.0, 0.0, 1.0, 1.0)]


def test_seq_increases_per_frame_and_frame_ids_pass_through() -> None:
    p = provider([])
    drafts = [d for i in range(3) for d in p.process(frame(i))]
    assert [(d.seq, getattr(d.payload, "frame_id", None)) for d in drafts] == [
        (0, "f0"),
        (1, "f1"),
        (2, "f2"),
    ]
    assert [d.capture_ts_monotonic_ns for d in drafts] == [0, 100_000_000, 200_000_000]


def test_invalid_items_are_rejected() -> None:
    p = provider([])
    with pytest.raises(ValueError):
        p.process({"frame_id": "f0"})
    with pytest.raises(ValueError):
        p.process(frame(size=(0, 480)))


def test_score_floor_and_labels_are_validated() -> None:
    with pytest.raises(ValueError):
        provider([], score_floor=1.5)
    with pytest.raises(ValueError):
        RtDetrProvider(FakeDetector([]), model_id=MODEL_ID, revision="r", labels=())


# --- weights: local file only, sha256 first (ADR-0007, SPEC 9) ---


def write_weights(root: Path, content: bytes = b"weights") -> str:
    folder = root / MODEL_ID
    folder.mkdir(parents=True)
    (folder / WEIGHTS_FILE).write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_verified_weights_return_the_model_folder(tmp_path: Path) -> None:
    digest = write_weights(tmp_path)
    assert verify_weights(MODEL_ID, digest, tmp_path) == tmp_path / MODEL_ID


def test_missing_or_tampered_weights_are_refused_without_loading(tmp_path: Path) -> None:
    with pytest.raises(WeightsError, match="missing"):
        verify_weights(MODEL_ID, "0" * 64, tmp_path)
    write_weights(tmp_path)
    with pytest.raises(WeightsError, match="sha256"):
        verify_weights(MODEL_ID, "0" * 64, tmp_path)


def test_missing_vision_extra_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = write_weights(tmp_path)
    real_import = builtins.__import__

    def no_vision(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".")[0] in {"torch", "transformers", "PIL"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_vision)
    manifest = manifest_for(digest)
    with pytest.raises(VisionUnavailable, match="vision"):
        load_rtdetr(manifest, tmp_path)


def manifest_for(digest: str) -> ModelManifest:
    manifests = load_manifests(CONFIG / "models")
    assert isinstance(manifests, dict)
    return manifests[MODEL_ID].model_copy(update={"sha256": digest})


# --- manifest and C0 profile (ADR-0007 sections 2, 3) ---


def test_manifest_pins_revision_hash_and_license() -> None:
    manifests = load_manifests(CONFIG / "models")
    assert isinstance(manifests, dict)
    manifest = manifests[MODEL_ID]
    assert manifest.kind == "object_detection" and manifest.labels == ["person"]
    assert manifest.weights == "file" and manifest.sha256 is not None
    assert len(manifest.revision) == 40  # a Hugging Face commit, not a branch name
    assert manifest.license == "Apache-2.0"


def test_c0_profile_loads_and_matches_the_manifest() -> None:
    profile = load_profile(CONFIG / "profiles" / "local_verify_c0.yaml")
    assert isinstance(profile, PolicyProfile)
    [scenario] = profile.scenarios
    assert scenario.id == "person_in_walkway"
    assert scenario.required_kinds == ["object_detection"]
    assert scenario.required_models == {"object_detection": MODEL_ID}
    manifests = load_manifests(CONFIG / "models")
    assert isinstance(manifests, dict)
    assert check_profile_models(profile, manifests) is None


# --- real weights (excluded by default) ---


@pytest.mark.model
def test_real_weights_load_and_detect_on_a_synthetic_image() -> None:
    from PIL import Image

    weights_root = Path(os.environ.get("VOA_MODEL_WEIGHTS_DIR", SERVER / "var" / "models"))
    manifests = load_manifests(CONFIG / "models")
    assert isinstance(manifests, dict)
    p = load_rtdetr(manifests[MODEL_ID], weights_root)
    image = Image.new("RGB", (640, 480), (128, 128, 128))  # in memory only
    [draft] = p.process(Frame(frame_id="f0", capture_ts_monotonic_ns=0, image=image))
    assert draft.payload.kind == "object_detection"
    assert all(d.label == "person" for d in dets(draft))
