"""WebSocket `/v1/streams/{session_id}` messages (SPEC 7.2).

Every message is a JSON text frame `{"type": ..., ...}`; `type` is the
discriminator and payloads sit under their named keys. Shared with Android
through the JSON Schema snapshots in contracts/schemas.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel

from voa.contracts.decision import Alert, RiskDecision
from voa.contracts.enums import RiskState
from voa.contracts.observation import Detection, ObservationDraft
from voa.contracts.types import MODEL_CONFIG, Id, UtcDatetime

# Admission results other than "accepted" (mirrors voa.engine.admission.AdmissionStatus;
# contracts may not import the engine, a test keeps the two in sync).
RejectedAdmission = Literal[
    "rejected_session_mismatch", "duplicate", "out_of_order", "stale_on_arrival", "config_error"
]
ErrorCode = Literal[
    "unsupported_message_type",
    "rejected_schema",
    "protocol_error",
    "stream_not_declared",
    "mission_not_selected",
]
# Reserved for Stage C/D (SPEC 7.2); the MVP answers unsupported_message_type.
RESERVED_TYPES = frozenset({"frame", "audio_chunk", "imu", "ptt_start", "ptt_end"})


# --- Client -> Server ---


class AlertSyncMessage(BaseModel):
    model_config = MODEL_CONFIG
    type: Literal["alert_sync"]
    last_alert_seq: int = Field(ge=0)


class ObservationMessage(BaseModel):
    model_config = MODEL_CONFIG
    type: Literal["observation"]
    observation: ObservationDraft


class AckMessage(BaseModel):
    model_config = MODEL_CONFIG
    type: Literal["ack"]
    alert_id: Id
    status: Literal["received", "played"]


ClientMessage = Annotated[
    AlertSyncMessage | ObservationMessage | AckMessage, Field(discriminator="type")
]
CLIENT_TYPES = frozenset({"alert_sync", "observation", "ack"})  # `auth` retired: ADR-0009


class ClientEnvelope(RootModel[ClientMessage]):
    """Parses any client message; used for the JSON Schema snapshot too."""


# --- Server -> Client ---


class AuthOkMessage(BaseModel):
    model_config = MODEL_CONFIG
    type: Literal["auth_ok"] = "auth_ok"
    session_id: Id
    server_time_utc: UtcDatetime


class AdmissionMessage(BaseModel):
    model_config = MODEL_CONFIG
    type: Literal["admission"] = "admission"
    seq: int = Field(ge=0)
    stream_id: Id
    status: RejectedAdmission


class DecisionMessage(BaseModel):
    model_config = MODEL_CONFIG
    type: Literal["decision"] = "decision"
    decision: RiskDecision


class AlertMessage(BaseModel):
    model_config = MODEL_CONFIG
    type: Literal["alert"] = "alert"
    alert_seq: int = Field(ge=1)
    alert: Alert
    risk_event_id: Id
    scenario_id: Id


class NoticeMessage(BaseModel):
    model_config = MODEL_CONFIG
    type: Literal["notice"] = "notice"
    notice_id: Id
    kind: Literal["degraded"]
    scenario_id: Id
    text: str = Field(min_length=1)


class FrameCounters(BaseModel):
    """Frame uploads seen in this session (SPEC 7.6)."""

    model_config = MODEL_CONFIG
    received: int = 0
    processed: int = 0
    skipped: int = 0  # 409 frame_busy
    rejected: int = 0  # 4xx other than busy


class HeartbeatMessage(BaseModel):
    model_config = MODEL_CONFIG
    type: Literal["heartbeat"] = "heartbeat"
    server_time_utc: UtcDatetime
    states: dict[str, RiskState]
    frames: FrameCounters | None = None


class FrameModelResult(BaseModel):
    model_config = MODEL_CONFIG
    model_id: Id
    observation_id: Id  # the same ID a warning lists in `evidence_ids`
    detections: list[Detection]


class FrameTiming(BaseModel):
    """Device capture time echoed back; the rest is server time (never subtract the two)."""

    model_config = MODEL_CONFIG
    capture_ts_monotonic_ns: int
    received_at_utc: UtcDatetime
    processing_started_at_utc: UtcDatetime
    completed_at_utc: UtcDatetime
    queue_ms: float
    provider_ms: float
    engine_us: int


class FrameResult(BaseModel):
    """Body of a 200 frame upload and of `frame_result` (SPEC 7.6, ADR-0009)."""

    model_config = MODEL_CONFIG
    frame_id: Id
    seq: int = Field(ge=0)
    status: Literal["processed"] = "processed"
    results: list[FrameModelResult]
    states: dict[str, RiskState]
    timing: FrameTiming


class FrameResultMessage(FrameResult):
    type: Literal["frame_result"] = "frame_result"


class MissionMessage(BaseModel):
    """The session's mission was set or changed (ADR-0010)."""

    model_config = MODEL_CONFIG
    type: Literal["mission"] = "mission"
    mission_id: Id
    version: int = Field(ge=1)
    name: str


class ErrorMessage(BaseModel):
    """`message` is a fixed text per code; client input is never echoed back."""

    model_config = MODEL_CONFIG
    type: Literal["error"] = "error"
    code: ErrorCode
    message: str


ServerMessage = Annotated[
    AuthOkMessage
    | AdmissionMessage
    | DecisionMessage
    | AlertMessage
    | NoticeMessage
    | HeartbeatMessage
    | MissionMessage
    | FrameResultMessage
    | ErrorMessage,
    Field(discriminator="type"),
]


class ServerEnvelope(RootModel[ServerMessage]):
    """Parses any server message; used for the JSON Schema snapshot and clients."""


ERROR_TEXT: dict[ErrorCode, str] = {
    "unsupported_message_type": "message type is not supported",
    "rejected_schema": "message does not match the schema",
    "protocol_error": "message is not allowed at this point",
    "stream_not_declared": "stream was not declared for this session",
    "mission_not_selected": "select a mission for this session first",
}


def error_message(code: ErrorCode) -> ErrorMessage:
    return ErrorMessage(code=code, message=ERROR_TEXT[code])
