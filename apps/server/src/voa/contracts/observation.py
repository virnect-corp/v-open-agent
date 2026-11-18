"""Observation 계약 (SPEC §3.3). 영상 계열 payload는 프레임 단위다(D-03)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_serializer, model_validator

from voa.contracts.enums import (
    ClockQuality,
    HealthStatus,
    MotionClass,
    QualityFlag,
    SourceKind,
    SpeakerRole,
    TrackQuality,
)
from voa.contracts.types import MODEL_CONFIG, BBox, Id, Score, UtcDatetime

SCHEMA_VERSION = "0.1.0"
VISION_KINDS: frozenset[SourceKind] = frozenset({"object_detection", "track_state"})
MODEL_DESCRIPTION = (
    "Model that produced this observation. Required when payload.kind is "
    "object_detection or track_state (ADR-0004); optional for other kinds."
)


class ModelRef(BaseModel):
    model_config = MODEL_CONFIG
    id: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class Detection(BaseModel):
    model_config = MODEL_CONFIG
    label: str = Field(min_length=1)
    class_id: int | None = None
    bbox: BBox
    score: Score


class DetectionPayload(BaseModel):
    model_config = MODEL_CONFIG
    kind: Literal["object_detection"]
    frame_id: Id
    detections: list[Detection]


class Track(BaseModel):
    model_config = MODEL_CONFIG
    track_id: Id
    label: str = Field(min_length=1)
    bbox: BBox
    score: Score
    quality: TrackQuality
    motion_class: MotionClass
    age_frames: int = Field(ge=1)


class TrackPayload(BaseModel):
    model_config = MODEL_CONFIG
    kind: Literal["track_state"]
    frame_id: Id
    tracks: list[Track]


class SoundEventPayload(BaseModel):
    model_config = MODEL_CONFIG
    kind: Literal["sound_event"]
    chunk_id: Id
    window_start_ns: int
    window_end_ns: int
    events: list[tuple[str, Score]]

    @model_validator(mode="after")
    def _window_order(self) -> "SoundEventPayload":
        if not self.window_start_ns < self.window_end_ns:
            raise ValueError("window_start_ns must be < window_end_ns")
        return self


class AsrPayload(BaseModel):
    model_config = MODEL_CONFIG
    kind: Literal["asr_transcript"]
    utterance_id: Id
    revision: int = Field(ge=0)
    is_final: bool
    text: str = Field(max_length=500)
    speaker_role: SpeakerRole
    authentication_basis: Literal["explicit_push_to_talk", "none"]


class OcrPayload(BaseModel):
    model_config = MODEL_CONFIG
    kind: Literal["ocr_text"]
    frame_id: Id
    text: str = Field(max_length=500)
    bbox: BBox
    language: str = Field(min_length=1)
    score: Score


class HealthPayload(BaseModel):
    """대상 센서는 Observation.stream_id 이다."""

    model_config = MODEL_CONFIG
    kind: Literal["health"]
    status: HealthStatus
    detail_code: str = Field(min_length=1)


Payload = Annotated[
    DetectionPayload | TrackPayload | SoundEventPayload | AsrPayload | OcrPayload | HealthPayload,
    Field(discriminator="kind"),
]


class _FlagsMixin(BaseModel):
    quality_flags: frozenset[QualityFlag] = frozenset()

    def _require_vision_model(self, model: "ModelRef | None", kind: SourceKind) -> None:
        # Several models may analyse one frame; each vision observation names its source.
        if model is None and kind in VISION_KINDS:
            raise ValueError("model is required for object_detection and track_state")

    @field_serializer("quality_flags")
    def _sorted_flags(self, v: frozenset[str]) -> list[str]:
        # 결정론적 직렬화 (INV-05): 집합은 정렬된 리스트로 출력
        return sorted(v)


class ObservationDraft(_FlagsMixin):
    """장치/시뮬레이터/Provider가 만드는 형태 (서버 필드 없음)."""

    model_config = MODEL_CONFIG
    stream_id: Id
    seq: int = Field(ge=0)
    capture_ts_monotonic_ns: int
    clock_quality: ClockQuality = "unknown"
    model: ModelRef | None = Field(default=None, description=MODEL_DESCRIPTION)
    payload: Payload

    @model_validator(mode="after")
    def _vision_model(self) -> "ObservationDraft":
        self._require_vision_model(self.model, self.payload.kind)
        return self


class Observation(_FlagsMixin):
    model_config = MODEL_CONFIG
    schema_version: Literal["0.1.0"]
    observation_id: Id
    session_id: Id
    device_id: Id
    stream_id: Id
    seq: int = Field(ge=0)
    capture_ts_monotonic_ns: int
    received_at_utc: UtcDatetime
    server_received_mono_ns: int
    clock_quality: ClockQuality
    model: ModelRef | None = Field(description=MODEL_DESCRIPTION)
    payload: Payload

    @model_validator(mode="after")
    def _vision_model(self) -> "Observation":
        self._require_vision_model(self.model, self.payload.kind)
        return self

    @property
    def source_kind(self) -> SourceKind:
        return self.payload.kind

    @classmethod
    def from_draft(
        cls,
        draft: ObservationDraft,
        *,
        observation_id: str,
        session_id: str,
        device_id: str,
        server_received_mono_ns: int,
        received_at_utc: datetime,
    ) -> "Observation":
        """Gateway(또는 재생기)가 서버 필드를 붙인다 (SPEC §3.3)."""
        data = draft.model_dump()
        data.update(
            schema_version=SCHEMA_VERSION,
            observation_id=observation_id,
            session_id=session_id,
            device_id=device_id,
            server_received_mono_ns=server_received_mono_ns,
            received_at_utc=received_at_utc,
        )
        return cls.model_validate(data)


def model_id_of(observation: Observation) -> str:
    """Model key of an observation: model.id, or "" when no model is named (SPEC 3.3)."""
    return observation.model.id if observation.model is not None else ""
