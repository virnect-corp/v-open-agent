"""Server-side detectors for uploaded frames (ADR-0009, SPEC 7.6).

A model is loaded the first time a session's mission needs it and is then shared
by every session. One lock per model serialises inference on it; sessions do not
queue frames (one in flight per stream, `409 frame_busy`). Builtin mock models
have no weights and see nothing, so a mock mission still runs end to end.
Real detectors get the JPEG bytes and decode them in memory (Pillow, `vision`
group); nothing is written or logged. Loading never downloads (ADR-0007).
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from voa.config.model_registry import ModelManifest
from voa.providers.rtdetr import (
    DEFAULT_SCORE_FLOOR,
    Detector,
    RawDetection,
    RtDetrProvider,
    VisionUnavailable,
    WeightsError,
)


class DetectorFactory(Protocol):
    def __call__(self, manifest: ModelManifest, weights_root: Path) -> Detector: ...


class NullDetector:
    """Builtin mock models: no pixels are looked at and nothing is detected."""

    def detect(self, image: Any) -> tuple[int, int, list[RawDetection]]:
        return 1, 1, []


@dataclass
class DecodingDetector:
    """Decode JPEG bytes in memory, then run the wrapped detector."""

    inner: Detector
    decode: Callable[[bytes], Any]

    def detect(self, image: Any) -> tuple[int, int, list[RawDetection]]:
        return self.inner.detect(self.decode(image))


def default_factory(manifest: ModelManifest, weights_root: Path) -> Detector:
    """Builtin -> NullDetector; file weights -> verified local weights (never downloaded)."""
    if manifest.weights == "builtin":
        return NullDetector()
    from voa.providers.detectors import load_detector
    from voa.simulator.frames import pillow_decode

    provider = load_detector(manifest, weights_root, score_floor=0.0)
    return DecodingDetector(provider.detector, pillow_decode)


class ModelUnavailable(RuntimeError):
    """The model has no manifest, its weights are missing or do not match, or `vision` is absent."""


@dataclass
class ModelPool:
    manifests: dict[str, ModelManifest]
    weights_root: Path
    factory: DetectorFactory = default_factory
    _detectors: dict[str, Detector] = field(default_factory=dict)
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def lock(self, model_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(model_id, threading.Lock())

    def detector(self, model_id: str) -> Detector:
        """Load once (blocking: call from a worker thread)."""
        with self._guard:
            found = self._detectors.get(model_id)
        if found is not None:
            return found
        manifest = self.manifests.get(model_id)
        if manifest is None or manifest.kind != "object_detection":
            raise ModelUnavailable(model_id)
        try:
            loaded = self.factory(manifest, self.weights_root)
        except (WeightsError, VisionUnavailable, ValueError, OSError) as error:
            raise ModelUnavailable(model_id) from error
        with self._guard:
            return self._detectors.setdefault(model_id, loaded)

    def provider(self, model_id: str, score_floor: float) -> RtDetrProvider:
        manifest = self.manifests[model_id]
        return RtDetrProvider(
            self.detector(model_id),
            model_id=model_id,
            revision=manifest.revision,
            labels=tuple(manifest.labels),
            score_floor=min(DEFAULT_SCORE_FLOOR, score_floor),
        )
