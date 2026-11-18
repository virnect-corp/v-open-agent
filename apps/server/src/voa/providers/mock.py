"""Scripted vision provider for fixtures and simulation (SPEC 5).

Each item is one frame script: the objects a detector (and tracker) would
report. Output is a detection draft, then a track draft for the same frame.
"""

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from voa.contracts.enums import MotionClass, QualityFlag, TrackQuality
from voa.contracts.observation import (
    Detection,
    DetectionPayload,
    ModelRef,
    ObservationDraft,
    Track,
    TrackPayload,
)
from voa.contracts.types import MODEL_CONFIG, BBox, Id, Score

MOCK_DETECTOR = ModelRef(id="mock-detector", revision="0")
MOCK_TRACKER = ModelRef(id="mock-tracker", revision="0")


class MockObject(BaseModel):
    model_config = MODEL_CONFIG
    label: str
    bbox: BBox
    score: Score
    track_id: Id | None = None  # default: trk_<index in frame>
    quality: TrackQuality = "good"
    motion_class: MotionClass = "unknown"
    age_frames: int = Field(default=1, ge=1)


class MockFrame(BaseModel):
    """Frame script. `quality_flags` go on both drafts, `track_flags` only on the track."""

    model_config = MODEL_CONFIG
    frame_id: Id
    capture_ts_monotonic_ns: int
    objects: list[MockObject] = Field(default_factory=list)
    quality_flags: frozenset[QualityFlag] = frozenset()
    track_flags: frozenset[QualityFlag] = frozenset()


@dataclass
class MockVisionProvider:
    stream_id: str = "camera_0"
    emit_tracks: bool = True
    _seq: int = field(default=0, init=False, repr=False)

    def process(self, item: object) -> list[ObservationDraft]:
        """Raises pydantic.ValidationError (a ValueError) without consuming a seq."""
        frame = MockFrame.model_validate(item)
        seq = self._seq
        drafts = [
            ObservationDraft(
                stream_id=self.stream_id,
                seq=seq,
                capture_ts_monotonic_ns=frame.capture_ts_monotonic_ns,
                model=MOCK_DETECTOR,
                quality_flags=frame.quality_flags,
                payload=DetectionPayload(
                    kind="object_detection",
                    frame_id=frame.frame_id,
                    detections=[
                        Detection(label=obj.label, bbox=obj.bbox, score=obj.score)
                        for obj in frame.objects
                    ],
                ),
            )
        ]
        if self.emit_tracks:
            drafts.append(
                ObservationDraft(
                    stream_id=self.stream_id,
                    seq=seq,
                    capture_ts_monotonic_ns=frame.capture_ts_monotonic_ns,
                    model=MOCK_TRACKER,
                    quality_flags=frame.quality_flags | frame.track_flags,
                    payload=TrackPayload(
                        kind="track_state",
                        frame_id=frame.frame_id,
                        tracks=[
                            Track(
                                track_id=obj.track_id or f"trk_{index}",
                                label=obj.label,
                                bbox=obj.bbox,
                                score=obj.score,
                                quality=obj.quality,
                                motion_class=obj.motion_class,
                                age_frames=obj.age_frames,
                            )
                            for index, obj in enumerate(frame.objects)
                        ],
                    ),
                )
            )
        self._seq += 1
        return drafts
