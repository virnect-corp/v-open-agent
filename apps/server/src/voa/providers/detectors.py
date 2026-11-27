"""Pick and load the detector for a model manifest (ADR-0007, ADR-0008; INP-05).

The weight file is checked against the manifest sha256 first. The detector
family then comes from `model_type` in the local `config.json` that ships with
the weights (`rt_detr` -> RT-DETR, `omdet-turbo` -> OmDet-Turbo), so the
manifest format (SPEC section 9) does not change. Nothing is downloaded and
error messages never contain paths.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from voa.providers.rtdetr import (
    DEFAULT_SCORE_FLOOR,
    Detector,
    ManifestLike,
    RtDetrProvider,
    VisionUnavailable,
    WeightsError,
    verify_weights,
)

DetectorFactory = Callable[[Path, Sequence[str], float, str], Detector]


def _rtdetr(folder: Path, labels: Sequence[str], score_floor: float, device: str) -> Detector:
    from voa.providers.rtdetr import TransformersRtDetr

    return TransformersRtDetr(folder, score_floor, device)


def _omdet(folder: Path, labels: Sequence[str], score_floor: float, device: str) -> Detector:
    if device != "cpu":  # exploration model only (ADR-0008 C1a): CPU
        raise VisionUnavailable("OmDet-Turbo runs on the CPU only")
    from voa.providers.omdet import TransformersOmDet

    return TransformersOmDet(folder, labels, score_floor)


FACTORIES: Mapping[str, DetectorFactory] = {"rt_detr": _rtdetr, "omdet-turbo": _omdet}


def model_type(model_id: str, folder: Path) -> str:
    try:
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WeightsError(f"{model_id}: config.json missing or unreadable") from error
    value = config.get("model_type") if isinstance(config, dict) else None
    if not isinstance(value, str):
        raise WeightsError(f"{model_id}: config.json has no model_type")
    return value


def load_detector(
    manifest: ManifestLike,
    weights_root: Path,
    *,
    stream_id: str = "camera_0",
    score_floor: float = DEFAULT_SCORE_FLOOR,
    factories: Mapping[str, DetectorFactory] = FACTORIES,
    device: str = "cpu",
) -> RtDetrProvider:
    """Verify the weights, choose the detector family, and wrap it as a provider."""
    if manifest.kind != "object_detection":
        raise ValueError("detectors emit object_detection only")
    if not 0.0 <= score_floor <= 1.0:
        raise ValueError("score_floor must be within [0, 1]")
    folder = verify_weights(manifest.model_id, manifest.sha256, weights_root)
    family = model_type(manifest.model_id, folder)
    factory = factories.get(family)
    if factory is None:
        raise WeightsError(f"{manifest.model_id}: unsupported model_type")
    labels = tuple(manifest.labels)
    return RtDetrProvider(
        factory(folder, labels, score_floor, device),
        model_id=manifest.model_id,
        revision=manifest.revision,
        labels=labels,
        stream_id=stream_id,
        score_floor=score_floor,
    )
