"""WebSocket `/v1/streams/{session_id}` and the per-session tick loop (SPEC 7.2, 7.4).

Authentication happens in the handshake (ADR-0009): the access token comes in
`Authorization: Bearer <token>` (native clients) or as the subprotocol
`voa.bearer.<token>` next to `voa.v1` (browsers cannot set headers). A missing,
invalid, expired, swept or foreign token is refused with HTTP 401 before the
socket opens; only `voa.v1` is echoed back. The token never appears in a URL,
a message or a log. Engine time is read immediately before each engine call
with no await in between, so ticks and observations never go back in time.
"""

import asyncio
import contextlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from voa.contracts.messages import (
    CLIENT_TYPES,
    ERROR_TEXT,
    AckMessage,
    AlertMessage,
    AlertSyncMessage,
    AuthOkMessage,
    ClientEnvelope,
    ClientMessage,
    ErrorCode,
    ErrorMessage,
    HeartbeatMessage,
    ObservationMessage,
    error_message,
)
from voa.contracts.observation import VISION_KINDS
from voa.server.session import SessionStream

if TYPE_CHECKING:
    from voa.server.app import ServerRuntime

CLOSE_UNAUTHORIZED = 4401  # server-side close of an authenticated socket (e.g. session swept)
CLOSE_CAPACITY = 4503  # session limit reached (ADR-0010)
SUBPROTOCOL = "voa.v1"
BEARER_PREFIX = "voa.bearer."


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
    # Errors carry capacity fields only for capacity_exceeded: leave them out otherwise.
    await websocket.send_text(
        message.model_dump_json(exclude_none=isinstance(message, ErrorMessage))
    )


async def push_messages(
    runtime: "ServerRuntime",
    stream: SessionStream,
    websocket: WebSocket,
    messages: Sequence[BaseModel],
) -> None:
    """Public entry for other routes (frame uploads, SPEC 7.6)."""
    await _push(runtime, stream, websocket, messages)


async def _push(
    runtime: "ServerRuntime",
    stream: SessionStream,
    websocket: WebSocket,
    messages: Sequence[BaseModel],
) -> None:
    """Send in order; an alert becomes `sent` only after its frame was written."""
    for message in messages:
        await _send(websocket, message)
        if isinstance(message, AlertMessage):
            stream.mark_sent(message.alert.alert_id, runtime.mono_ns())


async def tick_loop(runtime: "ServerRuntime", stream: SessionStream) -> None:
    """Runs from session creation; pushes to the connected socket if there is one."""
    interval = runtime.settings.tick_interval_ms / 1000
    while True:
        await asyncio.sleep(interval)
        messages = stream.tick(runtime.mono_ns())
        websocket = runtime.connections.get(stream.session_id)
        if websocket is None:
            continue  # alerts stay queued; alert_sync re-sends them if still valid
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await _push(runtime, stream, websocket, messages)


async def input_loop(runtime: "ServerRuntime", stream: SessionStream) -> None:
    """Consumes the bounded video queue; engine time is read right before each step."""
    while True:
        item = await stream.input.get()
        messages = stream.ingest(
            item.draft, runtime.mono_ns(), received_mono_ns=item.received_mono_ns
        )
        websocket = runtime.connections.get(stream.session_id)
        if websocket is None:
            continue
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await _push(runtime, stream, websocket, messages)


async def _heartbeat_loop(runtime: "ServerRuntime", session_id: str, websocket: WebSocket) -> None:
    interval = runtime.settings.heartbeat_ms / 1000
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await asyncio.sleep(interval)
            stream = runtime.streams.get(session_id)  # replaced when the mission changes
            frames = runtime.frame_states.get(session_id)
            beat = HeartbeatMessage(
                server_time_utc=runtime.clock.at(runtime.mono_ns()),
                states=dict(stream.engine.states()) if stream is not None else {},
                frames=frames.snapshot() if frames is not None else None,
            )
            await _send(websocket, beat)


def handshake_credentials(headers: Mapping[str, str]) -> tuple[str | None, str | None]:
    """(token, subprotocol to accept) from the upgrade request; (None, None) if absent.

    `Authorization: Bearer` wins. The subprotocol form needs `voa.v1` beside exactly
    one `voa.bearer.<token>`, so a browser always gets back the protocol it asked for.
    """
    offered = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",")]
    offered = [p for p in offered if p]
    accept = SUBPROTOCOL if SUBPROTOCOL in offered else None
    scheme, _, value = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip(), accept
    bearer = [p.removeprefix(BEARER_PREFIX) for p in offered if p.startswith(BEARER_PREFIX)]
    if accept is not None and len(bearer) == 1 and bearer[0]:
        return bearer[0], accept
    return None, None


async def _refuse(websocket: WebSocket) -> None:
    """HTTP 401 before the socket opens; servers without the denial extension get a 403."""
    try:
        await websocket.send_denial_response(
            JSONResponse({"code": "unauthorized"}, status_code=401)
        )
    except RuntimeError:
        await websocket.close(code=1008)


def register_stream_routes(app: FastAPI) -> None:
    @app.websocket("/v1/streams/{session_id}")
    async def stream_endpoint(websocket: WebSocket, session_id: str) -> None:
        runtime: ServerRuntime = websocket.app.state.runtime
        token, subprotocol = handshake_credentials(websocket.headers)
        if token is None or not runtime.sessions.verify_access(
            session_id, token, runtime.mono_ns()
        ):
            await _refuse(websocket)
            return
        await websocket.accept(subprotocol=subprotocol)
        if (
            session_id not in runtime.connections
            and len(runtime.connections) >= runtime.settings.max_sessions
        ):  # ADR-0010: tell the browser why, then close (its handshake cannot read a body)
            body = runtime.capacity_body()
            await websocket.send_text(
                ErrorMessage(
                    code="capacity_exceeded",
                    message=ERROR_TEXT["capacity_exceeded"],
                    active=int(str(body["active"])),
                    max=int(str(body["max"])),
                    retry_after_s=int(str(body["retry_after_s"])),
                ).model_dump_json(exclude_none=True)
            )
            await websocket.close(code=CLOSE_CAPACITY)
            return

        await _send(
            websocket,
            AuthOkMessage(
                session_id=session_id, server_time_utc=runtime.clock.at(runtime.mono_ns())
            ),
        )
        runtime.connections[session_id] = websocket
        runtime.idle_since.pop(session_id, None)  # TASK-47: connected, not idle
        heartbeat = asyncio.create_task(_heartbeat_loop(runtime, session_id, websocket))
        try:
            mission = runtime.mission_message(session_id)
            if mission is not None:
                await _send(websocket, mission)
            await _serve(runtime, session_id, websocket)
        except WebSocketDisconnect:
            pass
        finally:
            heartbeat.cancel()
            if runtime.connections.get(session_id) is websocket:
                del runtime.connections[session_id]
                if session_id in runtime.session_started:  # still a session: idle from now
                    runtime.idle_since[session_id] = runtime.mono_ns()
            if websocket.application_state != WebSocketState.DISCONNECTED:
                with contextlib.suppress(RuntimeError):
                    await websocket.close(code=CLOSE_UNAUTHORIZED)


async def _serve(runtime: "ServerRuntime", session_id: str, websocket: WebSocket) -> None:
    synced = False
    while True:
        event = await websocket.receive()
        if event["type"] == "websocket.disconnect":
            return
        message = parse_client_message(event)
        stream = runtime.streams.get(session_id)  # current mission's engine (ADR-0010)
        if isinstance(message, str):
            await _send(websocket, error_message(message))
        elif stream is None:
            if isinstance(message, ObservationMessage):
                await _send(websocket, error_message("mission_not_selected"))
            elif isinstance(message, AlertSyncMessage):
                synced = True  # nothing to re-send before a mission exists
        elif isinstance(message, AlertSyncMessage):
            if synced:  # once per connection (SPEC 7.2)
                await _send(websocket, error_message("protocol_error"))
                continue
            synced = True
            resend = stream.alert_sync(message.last_alert_seq, runtime.mono_ns())
            await _push(runtime, stream, websocket, resend)
        elif isinstance(message, AckMessage):
            # Unknown alert IDs are ignored; ACKs are idempotent (SPEC 7.3).
            stream.ack(message.alert_id, message.status, runtime.mono_ns())
        elif isinstance(message, ObservationMessage):
            draft = message.observation
            if not stream.accepts_stream(draft.stream_id):
                await _send(websocket, error_message("stream_not_declared"))
                continue
            now = runtime.mono_ns()
            stream.record_receipt(draft.stream_id, now)
            if draft.payload.kind in VISION_KINDS:
                stream.input.offer(draft, received_mono_ns=now)  # overload policy: SPEC 7.5
            else:
                await _push(runtime, stream, websocket, stream.ingest(draft, now))  # control path
