"""WebSocket `/v1/streams/{session_id}` and the per-session tick loop (SPEC 7.2, 7.4).

The first message must be `auth` within AUTH_TIMEOUT_S; otherwise, or on a bad
token, the socket closes with 4401. Engine time is read immediately before each
engine call with no await in between, so ticks and observations never go back
in time.
"""

import asyncio
import contextlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket
from pydantic import BaseModel, ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from voa.contracts.messages import (
    CLIENT_TYPES,
    AckMessage,
    AuthMessage,
    AuthOkMessage,
    ClientEnvelope,
    ClientMessage,
    ErrorCode,
    HeartbeatMessage,
    ObservationMessage,
    ResumeMessage,
    error_message,
)
from voa.server.session import SessionStream

if TYPE_CHECKING:
    from voa.server.app import ServerRuntime

AUTH_TIMEOUT_S = 5.0
CLOSE_UNAUTHORIZED = 4401


def parse_client_message(event: Mapping[str, Any]) -> ClientMessage | ErrorCode:
    """Binary, non-JSON, unknown and reserved types are unsupported; bad shapes are rejected."""
    text = event.get("text")
    if not isinstance(text, str):
        return "unsupported_message_type"
    try:
        data = json.loads(text)
    except ValueError:
        return "unsupported_message_type"
    if not isinstance(data, dict) or data.get("type") not in CLIENT_TYPES:
        return "unsupported_message_type"
    try:
        return ClientEnvelope.model_validate(data).root
    except ValidationError:
        return "rejected_schema"


async def _send(websocket: WebSocket, message: BaseModel) -> None:
    await websocket.send_text(message.model_dump_json())


async def tick_loop(runtime: "ServerRuntime", stream: SessionStream) -> None:
    """Runs from session creation; pushes to the connected socket if there is one."""
    interval = runtime.settings.tick_interval_ms / 1000
    while True:
        await asyncio.sleep(interval)
        messages = stream.tick(runtime.mono_ns())
        websocket = runtime.connections.get(stream.session_id)
        if websocket is None:
            continue  # delivery while disconnected is handled on resume (TASK-20)
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            for message in messages:
                await _send(websocket, message)


async def _heartbeat_loop(
    runtime: "ServerRuntime", stream: SessionStream, websocket: WebSocket
) -> None:
    interval = runtime.settings.heartbeat_ms / 1000
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await asyncio.sleep(interval)
            beat = HeartbeatMessage(
                server_time_utc=runtime.clock.at(runtime.mono_ns()),
                states=dict(stream.engine.states()),
            )
            await _send(websocket, beat)


async def _authenticate(
    runtime: "ServerRuntime", websocket: WebSocket, session_id: str
) -> SessionStream | None:
    try:
        event = await asyncio.wait_for(websocket.receive(), AUTH_TIMEOUT_S)
    except TimeoutError:
        return None
    message = parse_client_message(event)
    if isinstance(message, AuthMessage):
        stream = runtime.streams.get(session_id)
        if stream is not None and runtime.sessions.verify_access(
            session_id, message.token, runtime.mono_ns()
        ):
            return stream
        return None
    if event.get("type") == "websocket.receive":
        code: ErrorCode = "protocol_error" if not isinstance(message, str) else message
        await _send(websocket, error_message(code))
    return None


def register_stream_routes(app: FastAPI) -> None:
    @app.websocket("/v1/streams/{session_id}")
    async def stream_endpoint(websocket: WebSocket, session_id: str) -> None:
        runtime: ServerRuntime = websocket.app.state.runtime
        await websocket.accept()
        stream = await _authenticate(runtime, websocket, session_id)
        if stream is None:
            await websocket.close(code=CLOSE_UNAUTHORIZED)
            return

        await _send(
            websocket,
            AuthOkMessage(
                session_id=session_id, server_time_utc=runtime.clock.at(runtime.mono_ns())
            ),
        )
        runtime.connections[session_id] = websocket
        heartbeat = asyncio.create_task(_heartbeat_loop(runtime, stream, websocket))
        try:
            await _serve(runtime, stream, websocket)
        except WebSocketDisconnect:
            pass
        finally:
            heartbeat.cancel()
            if runtime.connections.get(session_id) is websocket:
                del runtime.connections[session_id]
            if websocket.application_state != WebSocketState.DISCONNECTED:
                with contextlib.suppress(RuntimeError):
                    await websocket.close(code=CLOSE_UNAUTHORIZED)


async def _serve(runtime: "ServerRuntime", stream: SessionStream, websocket: WebSocket) -> None:
    while True:
        event = await websocket.receive()
        if event["type"] == "websocket.disconnect":
            return
        message = parse_client_message(event)
        if isinstance(message, str):
            await _send(websocket, error_message(message))
        elif isinstance(message, AuthMessage):
            await _send(websocket, error_message("protocol_error"))
            return  # second auth: close 4401 in the caller
        elif isinstance(message, ResumeMessage):
            stream.last_alert_seq = message.last_alert_seq
        elif isinstance(message, AckMessage):
            pass  # delivery state transitions are TASK-19
        elif isinstance(message, ObservationMessage):
            draft = message.observation
            if not stream.accepts_stream(draft.stream_id):
                await _send(websocket, error_message("stream_not_declared"))
                continue
            for push in stream.ingest(draft, runtime.mono_ns()):
                await _send(websocket, push)
