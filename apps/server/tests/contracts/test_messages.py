"""TASK-18 WebSocket message contract (SPEC 7.2; COM-02, CON-02)."""

from typing import get_args

import pytest
from pydantic import ValidationError

from tests.contracts.snapshot import assert_schema_snapshot
from voa.contracts.messages import (
    ERROR_TEXT,
    AckMessage,
    ClientEnvelope,
    ErrorCode,
    ObservationMessage,
    RejectedAdmission,
    ServerEnvelope,
    error_message,
)
from voa.engine.admission import AdmissionStatus


def test_rejected_admission_mirrors_engine_statuses() -> None:
    assert set(get_args(RejectedAdmission)) == set(get_args(AdmissionStatus)) - {"accepted"}


def test_client_messages_parse_by_type() -> None:
    draft = {
        "stream_id": "camera_0",
        "seq": 0,
        "capture_ts_monotonic_ns": 0,
        "model": {"id": "mock-detector", "revision": "0"},
        "payload": {"kind": "object_detection", "frame_id": "f0", "detections": []},
    }
    parsed = ClientEnvelope.model_validate({"type": "observation", "observation": draft}).root
    assert isinstance(parsed, ObservationMessage)
    ack = ClientEnvelope.model_validate({"type": "ack", "alert_id": "a1", "status": "played"})
    assert isinstance(ack.root, AckMessage)


@pytest.mark.parametrize(
    "data",
    [
        {"type": "auth", "token": "t"},  # retired by ADR-0009: handshake authentication
        {"type": "alert_sync", "last_alert_seq": -1},
        {"type": "ack", "alert_id": "a1", "status": "sent"},
        {"type": "frame", "jpeg": "..."},
    ],
)
def test_invalid_client_messages_are_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ClientEnvelope.model_validate(data)


def test_server_messages_round_trip() -> None:
    for data in (
        {"type": "auth_ok", "session_id": "s1", "server_time_utc": "2026-11-09T00:00:00Z"},
        {"type": "admission", "seq": 3, "stream_id": "camera_0", "status": "duplicate"},
        {
            "type": "heartbeat",
            "server_time_utc": "2026-11-09T00:00:00Z",
            "states": {"x": "unknown"},
        },
        {"type": "error", "code": "rejected_schema", "message": "m"},
    ):
        message = ServerEnvelope.model_validate(data)
        assert ServerEnvelope.model_validate_json(message.model_dump_json()) == message


def test_error_messages_have_fixed_text_per_code() -> None:
    assert set(ERROR_TEXT) == set(get_args(ErrorCode))
    assert error_message("protocol_error").message == ERROR_TEXT["protocol_error"]


def test_message_schema_snapshots() -> None:
    assert_schema_snapshot(ClientEnvelope, "ws_client_message")
    assert_schema_snapshot(ServerEnvelope, "ws_server_message")
