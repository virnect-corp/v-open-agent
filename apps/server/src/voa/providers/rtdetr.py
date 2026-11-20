"""RT-DETR detector provider for Stage C0 local verification (ADR-0007; INP-03, INP-05).

`RtDetrProvider` turns one decoded frame into one `object_detection` Draft:
labels outside the manifest and scores below the floor are dropped, pixel boxes
are clamped to the frame and normalized. A frame where nothing is seen still
yields a Draft with no detections (evidence of absence, not missing input).

The model itself sits behind the small `Detector` port so the provider runs and
is tested without torch. `load_rtdetr` builds the real detector: it checks the
local weight file against the manifest sha256 *before* importing torch or
transformers, loads with `local_files_only=True`, and never downloads.
Images are only read in memory; nothing is written or logged.
"""

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from voa.contracts.observation import Detection, DetectionPayload, ModelRef, ObservationDraft

WEIGHTS_FILE = "model.safetensors"
DEFAULT_SCORE_FLOOR = 0.25  # verification default, revisited with OQ-06
_CHUNK = 1 << 20


class VisionUnavailable(RuntimeError):
    """The optional `vision` dependencies are not installed."""


class WeightsError(RuntimeError):
    """The local weight file is missing or does not match the manifest."""


class RawDetection(NamedTuple):
    label: str
    score: float
    box: tuple[float, float, float, float]  # pixel x1, y1, x2, y2


class Detector(Protocol):
    def detect(self, image: Any) -> tuple[int, int, list[RawDetection]]:
        """Return (width, height, detections in pixel coordinates)."""
        ...


class ManifestLike(Protocol):
    """The manifest fields this provider needs (voa.config.model_registry.ModelManifest)."""

    @property
    def model_id(self) -> str: ...
    @property
    def revision(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def labels(self) -> list[str]: ...
    @property
    def sha256(self) -> str | None: ...


@dataclass(frozen=True)
class Frame:
    """One decoded camera frame; `image` is whatever the detector accepts (PIL image)."""

    frame_id: str
    capture_ts_monotonic_ns: int
    image: Any


def _normalized(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[float, float, float, float] | None:
    x1, y1, x2, y2 = (float(v) for v in box)
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        return None
    nx1, nx2 = (min(max(v / width, 0.0), 1.0) for v in (x1, x2))
    ny1, ny2 = (min(max(v / height, 0.0), 1.0) for v in (y1, y2))
    if not (nx1 < nx2 and ny1 < ny2):
        return None
    return (nx1, ny1, nx2, ny2)


@dataclass
class RtDetrProvider:
    detector: Detector
    model_id: str
    revision: str
    labels: Sequence[str]
    stream_id: str = "camera_0"
    score_floor: float = DEFAULT_SCORE_FLOOR
    _seq: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.labels:
            raise ValueError("labels must not be empty")
        if not 0.0 <= self.score_floor <= 1.0:
            raise ValueError("score_floor must be within [0, 1]")
        self._labels = frozenset(self.labels)

    def process(self, item: object) -> list[ObservationDraft]:
        if not isinstance(item, Frame):
            raise ValueError("expected a Frame")
        width, height, raw = self.detector.detect(item.image)
        if width <= 0 or height <= 0:
            raise ValueError("frame has no pixels")
        detections = []
        for found in raw:
            score = float(found.score)
            if found.label not in self._labels or not math.isfinite(score):
                continue
            if score < self.score_floor:
                continue
            bbox = _normalized(found.box, width, height)
            if bbox is None:
                continue
            detections.append(Detection(label=found.label, bbox=bbox, score=min(score, 1.0)))
        draft = ObservationDraft(
            stream_id=self.stream_id,
            seq=self._seq,
            capture_ts_monotonic_ns=item.capture_ts_monotonic_ns,
            model=ModelRef(id=self.model_id, revision=self.revision),
            payload=DetectionPayload(
                kind="object_detection", frame_id=item.frame_id, detections=detections
            ),
        )
        self._seq += 1
        return [draft]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_weights(model_id: str, sha256: str | None, weights_root: Path) -> Path:
    """`<weights_root>/<model_id>/model.safetensors` must exist and match the manifest."""
    folder = Path(weights_root) / model_id
    path = folder / WEIGHTS_FILE
    if not path.is_file():
        raise WeightsError(f"{model_id}: weights file missing")
    if sha256 is None or _sha256(path) != sha256:
        raise WeightsError(f"{model_id}: sha256 does not match the manifest")
    return folder


class TransformersRtDetr:
    """RTDetrForObjectDetection on CPU via the official image processor."""

    def __init__(self, folder: Path, score_floor: float) -> None:
        try:
            import torch
            from transformers import RTDetrForObjectDetection, RTDetrImageProcessor
        except ImportError as error:
            raise VisionUnavailable(
                "RT-DETR needs the optional 'vision' dependencies (run in scripts/vision.sh)"
            ) from error
        self._torch = torch
        self._processor = RTDetrImageProcessor.from_pretrained(folder, local_files_only=True)
        self._model = RTDetrForObjectDetection.from_pretrained(folder, local_files_only=True)
        self._model.eval()
        self._score_floor = score_floor

    def detect(self, image: Any) -> tuple[int, int, list[RawDetection]]:
        width, height = image.size
        torch = self._torch
        inputs = self._processor(images=image, return_tensors="pt")
        with torch.inference_mode():
            outputs = self._model(**inputs)
        [result] = self._processor.post_process_object_detection(
            outputs, target_sizes=torch.tensor([(height, width)]), threshold=self._score_floor
        )
        names = self._model.config.id2label
        found = [
            RawDetection(
                str(names[int(label)]),
                float(score),
                (float(box[0]), float(box[1]), float(box[2]), float(box[3])),
            )
            for score, label, box in zip(
                result["scores"].tolist(),
                result["labels"].tolist(),
                result["boxes"].tolist(),
                strict=True,
            )
        ]
        return int(width), int(height), found


def load_rtdetr(
    manifest: ManifestLike,
    weights_root: Path,
    *,
    stream_id: str = "camera_0",
    score_floor: float = DEFAULT_SCORE_FLOOR,
) -> RtDetrProvider:
    """Verify the local weights first, then load the model (never downloads)."""
    if manifest.kind != "object_detection":
        raise ValueError("RT-DETR emits object_detection only")
    folder = verify_weights(manifest.model_id, manifest.sha256, weights_root)
    return RtDetrProvider(
        TransformersRtDetr(folder, score_floor),
        model_id=manifest.model_id,
        revision=manifest.revision,
        labels=tuple(manifest.labels),
        stream_id=stream_id,
        score_floor=score_floor,
    )
