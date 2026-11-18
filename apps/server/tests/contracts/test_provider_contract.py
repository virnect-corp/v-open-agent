"""CON-03 provider contract suite: every ObservationProvider must pass it.

MockVisionProvider and an independent test double run the same checks. Stage C
model adapters are added to PROVIDERS and must pass unchanged.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from voa.contracts.observation import (
    Detection,
    DetectionPayload,
    ModelRef,
    Observation,
    ObservationDraft,
    TrackPayload,
)
from voa.engine.ports import FixedEpochClock
from voa.providers.base import ObservationProvider
from voa.providers.mock import MockVisionProvider


@dataclass
class LabelListDouble:
    """Independent double: item = {"ts": int, "labels": [str, ...]}, detection only."""

    _seq: int = field(default=0, init=False)

    def process(self, item: object) -> list[ObservationDraft]:
        if not isinstance(item, dict) or not isinstance(item.get("ts"), int):
            raise ValueError("expected {'ts': int, 'labels': [...]}")
        labels = item.get("labels", [])
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            raise ValueError("labels must be a list of str")
        draft = ObservationDraft(
            stream_id="camera_9",
            seq=self._seq,
            capture_ts_monotonic_ns=item["ts"],
            model=ModelRef(id="label-list-double", revision="1"),
            payload=DetectionPayload(
                kind="object_detection",
                frame_id=f"d{self._seq}",
                detections=[
                    Detection(label=label, bbox=(0.1, 0.1, 0.2, 0.2), score=0.5) for label in labels
                ],
            ),
        )
        self._seq += 1
        return [draft]


@dataclass(frozen=True)
class Case:
    make: Callable[[], ObservationProvider]
    items: list[object]  # valid, increasing capture times
    empty: object  # a processed frame in which nothing was seen
    invalid: object


def _frame(i: int, objects: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "frame_id": f"f{i}",
        "capture_ts_monotonic_ns": i * 100_000_000,
        "objects": objects if objects is not None else [],
    }


PERSON = {
    "label": "person",
    "bbox": [0.3, 0.4, 0.7, 0.9],
    "score": 0.9,
    "track_id": "trk_0",
    "motion_class": "moving",
}

PROVIDERS = {
    "mock_vision": Case(
        make=MockVisionProvider,
        items=[_frame(0, [PERSON]), _frame(1, [PERSON]), _frame(2)],
        empty=_frame(7),
        invalid={"frame_id": "f0", "objects": [{"label": "x", "bbox": [0.5, 0.5, 0.1, 0.1]}]},
    ),
    "label_list_double": Case(
        make=LabelListDouble,
        items=[{"ts": 0, "labels": ["obstacle"]}, {"ts": 5, "labels": []}],
        empty={"ts": 9, "labels": []},
        invalid={"labels": "obstacle"},
    ),
}


@pytest.fixture(params=sorted(PROVIDERS))
def case(request: pytest.FixtureRequest) -> Case:
    return PROVIDERS[request.param]


def run(case: Case, items: list[object]) -> list[ObservationDraft]:
    provider = case.make()
    return [draft for item in items for draft in provider.process(item)]


def test_satisfies_protocol_and_returns_drafts(case: Case) -> None:
    provider = case.make()
    assert isinstance(provider, ObservationProvider)
    drafts = run(case, case.items)
    assert drafts and all(type(d) is ObservationDraft for d in drafts)


def test_seq_strictly_increases_per_stream_and_kind(case: Case) -> None:
    last: dict[tuple[str, str], int] = {}
    for draft in run(case, case.items):
        key = (draft.stream_id, draft.payload.kind)
        assert draft.seq > last.get(key, -1)
        last[key] = draft.seq


def test_vision_drafts_always_name_their_model(case: Case) -> None:
    """ADR-0004: several models may analyse one frame, so each draft names its source."""
    for draft in run(case, case.items):
        if draft.payload.kind in ("object_detection", "track_state"):
            assert draft.model is not None and draft.model.id


def test_capture_time_comes_from_input_not_a_clock(case: Case) -> None:
    """Delay safety: providers never stamp wall-clock or arrival time."""
    first, second = run(case, case.items), run(case, case.items)
    assert [d.capture_ts_monotonic_ns for d in first] == [d.capture_ts_monotonic_ns for d in second]
    times = [d.capture_ts_monotonic_ns for d in first]
    assert times == sorted(times)


def test_deterministic_for_fresh_instances(case: Case) -> None:
    assert [d.model_dump_json() for d in run(case, case.items)] == [
        d.model_dump_json() for d in run(case, case.items)
    ]


def test_seeing_nothing_still_reports_the_processed_frame(case: Case) -> None:
    """Missing objects is an empty list, never a dropped frame (absence needs evidence)."""
    drafts = case.make().process(case.empty)
    assert drafts
    for draft in drafts:
        payload = draft.payload
        assert isinstance(payload, DetectionPayload | TrackPayload)
        items = payload.detections if isinstance(payload, DetectionPayload) else payload.tracks
        assert items == []


def test_invalid_item_raises_and_emits_nothing(case: Case) -> None:
    provider = case.make()
    with pytest.raises(ValueError):
        provider.process(case.invalid)
    assert provider.process(case.items[0])


def test_drafts_are_gateway_compatible(case: Case) -> None:
    clock = FixedEpochClock()
    for n, draft in enumerate(run(case, case.items)):
        observation = Observation.from_draft(
            draft,
            observation_id=f"obs_{n:04d}",
            session_id="sess_c",
            device_id="dev_c",
            server_received_mono_ns=draft.capture_ts_monotonic_ns,
            received_at_utc=clock.at(draft.capture_ts_monotonic_ns),
        )
        assert observation.source_kind == draft.payload.kind


# --- MockVisionProvider specifics ---


def test_mock_emits_detection_then_track_with_shared_frame() -> None:
    provider = MockVisionProvider()
    item = _frame(3, [PERSON]) | {
        "quality_flags": ["low_light"],
        "track_flags": ["camera_motion_uncompensated"],
    }
    detection, track = provider.process(item)
    assert isinstance(detection.payload, DetectionPayload)
    assert isinstance(track.payload, TrackPayload)
    assert detection.payload.frame_id == track.payload.frame_id == "f3"
    assert detection.stream_id == track.stream_id == "camera_0"
    assert detection.payload.detections[0].label == "person"
    assert track.payload.tracks[0].track_id == "trk_0"
    assert track.payload.tracks[0].motion_class == "moving"
    assert track.payload.tracks[0].quality == "good"
    assert detection.quality_flags == frozenset({"low_light"})
    assert track.quality_flags == frozenset({"low_light", "camera_motion_uncompensated"})
    assert detection.model is not None and detection.model.id == "mock-detector"
    assert track.model is not None and track.model.id == "mock-tracker"


def test_mock_detection_only_mode_and_default_track_ids() -> None:
    provider = MockVisionProvider(emit_tracks=False)
    (only,) = provider.process(
        _frame(0, [{"label": "obstacle", "bbox": [0.3, 0.5, 0.6, 0.9], "score": 0.9}])
    )
    assert only.payload.kind == "object_detection"
    tracked = MockVisionProvider().process(
        _frame(0, [{"label": "a", "bbox": [0.1, 0.1, 0.2, 0.2], "score": 0.5}] * 2)
    )[1]
    assert isinstance(tracked.payload, TrackPayload)
    assert [t.track_id for t in tracked.payload.tracks] == ["trk_0", "trk_1"]
