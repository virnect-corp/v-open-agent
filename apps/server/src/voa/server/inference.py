"""Server-side detectors for uploaded frames (ADR-0009, SPEC 7.6).

A model is loaded the first time a session's mission needs it and is then shared
by every session. One lock per model serialises inference on it; sessions do not
queue frames (one in flight per stream, `409 frame_busy`). Builtin mock models
have no weights and see nothing, so a mock mission still runs end to end.
Real detectors get the JPEG bytes and decode them in memory (Pillow, `vision`
group); nothing is written or logged. Loading never downloads (ADR-0007).
"""

import asyncio
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from voa.config.model_registry import ModelManifest
from voa.providers.rtdetr import (
    DEFAULT_SCORE_FLOOR,
    WEIGHTS_FILE,
    Detector,
    RawDetection,
    RtDetrProvider,
    VisionUnavailable,
    WeightsError,
    verify_weights,
)

Device = Literal["cpu", "cuda"]
WeightsStatus = Literal["ready", "missing", "sha256_mismatch"]


@dataclass(frozen=True)
class Deployment:
    device: Device = "cpu"
    score_floor: float = DEFAULT_SCORE_FLOOR


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
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)
    _loading: dict[str, threading.Lock] = field(default_factory=dict)
    _recent: dict[str, deque[int]] = field(default_factory=dict)
    deployments: dict[str, Deployment] = field(default_factory=dict)
    _weights_cache: dict[str, tuple[tuple[int, int], WeightsStatus]] = field(default_factory=dict)

    def lock(self, model_id: str) -> asyncio.Lock:
        """One inference at a time per model. asyncio.Lock wakes waiters in arrival order,
        and each session has at most one frame in flight per stream, so sessions take
        turns (round-robin, ADR-0010) and one busy session cannot starve another."""
        return self._locks.setdefault(model_id, asyncio.Lock())

    def record(self, model_id: str, now_ns: int) -> None:
        self._recent.setdefault(model_id, deque()).append(now_ns)

    def per_minute(self, now_ns: int) -> dict[str, int]:
        """Inferences per model over the last minute (GET /v1/admin/capacity)."""
        counts = {}
        for model_id, times in self._recent.items():
            while times and now_ns - times[0] > 60_000_000_000:
                times.popleft()
            counts[model_id] = len(times)
        return counts

    def detector(self, model_id: str) -> Detector:
        """Load once (blocking: call from a worker thread).

        One load per model at a time (TASK-48): when the mission warm-up and the first
        frame ask together, the later caller waits for the load in progress and gets its
        result instead of loading the model a second time. A failed load is not cached,
        so the next caller tries again. Different models load in parallel.
        """
        with self._guard:
            found = self._detectors.get(model_id)
            loading = self._loading.setdefault(model_id, threading.Lock())
        if found is not None:
            return found
        manifest = self.manifests.get(model_id)
        if manifest is None or manifest.kind != "object_detection":
            raise ModelUnavailable(model_id)
        with loading:
            with self._guard:
                found = self._detectors.get(model_id)
            if found is not None:
                return found  # loaded by the caller we waited for
            try:
                loaded = self.factory(manifest, self.weights_root)
            except (WeightsError, VisionUnavailable, ValueError, OSError) as error:
                raise ModelUnavailable(model_id) from error
            with self._guard:
                self.deployments.setdefault(model_id, Deployment())  # loaded = deployed
                return self._detectors.setdefault(model_id, loaded)

    def deploy(self, model_id: str, deployment: Deployment) -> None:
        """Load (blocking: call from a worker thread) and record the deployment settings."""
        self.detector(model_id)
        with self._guard:
            self.deployments[model_id] = deployment

    def undeploy(self, model_id: str) -> bool:
        with self._guard:
            self._detectors.pop(model_id, None)
            return self.deployments.pop(model_id, None) is not None

    def weights_status(self, model_id: str) -> WeightsStatus:
        """Check the local weights against the manifest without loading (cached by size/mtime)."""
        manifest = self.manifests[model_id]
        if manifest.weights == "builtin":
            return "ready"
        path = self.weights_root / model_id / WEIGHTS_FILE
        try:
            stat = path.stat()
        except OSError:
            return "missing"
        key = (stat.st_size, stat.st_mtime_ns)
        cached = self._weights_cache.get(model_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            verify_weights(model_id, manifest.sha256, self.weights_root)
            status: WeightsStatus = "ready"
        except WeightsError:
            status = "sha256_mismatch"
        self._weights_cache[model_id] = (key, status)
        return status

    def provider(self, model_id: str, score_floor: float) -> RtDetrProvider:
        manifest = self.manifests[model_id]
        return RtDetrProvider(
            self.detector(model_id),
            model_id=model_id,
            revision=manifest.revision,
            labels=tuple(manifest.labels),
            score_floor=min(self.deployments.get(model_id, Deployment()).score_floor, score_floor),
        )
