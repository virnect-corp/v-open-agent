"""TASK-48 one load per model: warm-up and the first frame do not load twice (ADR-0009)."""

import threading
import time
from pathlib import Path

import pytest

from voa.config.model_registry import ModelManifest, load_manifests
from voa.providers.rtdetr import RawDetection, WeightsError
from voa.server.inference import ModelPool, ModelUnavailable

MODELS = Path(__file__).resolve().parents[2] / "config" / "models"


class Detector:
    def detect(self, image: object) -> tuple[int, int, list[RawDetection]]:
        return 1, 1, []


class SlowFactory:
    """Like a real model load: slow, and it breaks when two loads overlap."""

    def __init__(self, fail_first: bool = False) -> None:
        self.calls = 0
        self.active: dict[str, int] = {}
        self.fail_first = fail_first
        self.lock = threading.Lock()

    def __call__(self, manifest: ModelManifest, weights_root: Path) -> Detector:
        model_id = manifest.model_id
        with self.lock:
            self.calls += 1
            self.active[model_id] = self.active.get(model_id, 0) + 1
            call = self.calls
        try:
            time.sleep(0.2)
            if self.active[model_id] > 1:
                raise ValueError("concurrent load of the same model")
            if self.fail_first and call == 1:
                raise WeightsError("missing")
            return Detector()
        finally:
            with self.lock:
                self.active[model_id] -= 1


def pool(factory: SlowFactory) -> ModelPool:
    manifests = load_manifests(MODELS)
    assert isinstance(manifests, dict)
    return ModelPool(manifests, Path("."), factory)


def test_warm_up_and_the_first_frame_share_one_load() -> None:
    factory = SlowFactory()
    models = pool(factory)
    results: list[object] = []
    errors: list[BaseException] = []

    def load() -> None:
        try:
            results.append(models.detector("mock-detector"))
        except BaseException as error:  # noqa: BLE001 - collected for the assertion
            errors.append(error)

    threads = [threading.Thread(target=load) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert factory.calls == 1
    assert results[0] is results[1] is results[2]


def test_a_failed_load_is_not_cached_and_can_be_retried() -> None:
    factory = SlowFactory(fail_first=True)
    models = pool(factory)
    with pytest.raises(ModelUnavailable):
        models.detector("mock-detector")
    assert models.detector("mock-detector") is not None
    assert factory.calls == 2


def test_different_models_load_independently() -> None:
    factory = SlowFactory()
    models = pool(factory)
    loaded: list[object] = []
    begin = time.monotonic()
    threads = [
        threading.Thread(target=lambda m=model_id: loaded.append(models.detector(m)))
        for model_id in ("mock-detector", "rtdetr-r18-dfire")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert factory.calls == 2 and len(loaded) == 2
    assert time.monotonic() - begin < 0.6  # in parallel, not one after the other (0.4 s)
