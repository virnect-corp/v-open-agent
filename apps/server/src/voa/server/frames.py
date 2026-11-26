"""`POST /v1/sessions/{session_id}/frames`: server-side inference (ADR-0009, SPEC 7.6).

Order: session token, mission and control WebSocket, headers and JPEG checks,
per-stream order, one frame in flight per stream (`409 frame_busy`, no queue),
detection off the event loop, then `DecisionEngine.step` on the loop for each
model's Draft. Within `VOA_FRAME_SYNC_TIMEOUT_MS` the result is the 200 body;
later it goes out as `frame_result` on the control socket after a `202`.
Frame bytes live only in this request: never stored, copied or logged.
"""

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, get_args

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from voa.contracts.enums import QualityFlag
from voa.contracts.messages import (
    ConfigMessage,
    FrameCounters,
    FrameModelResult,
    FrameResult,
    FrameResultMessage,
    FrameTiming,
)
from voa.contracts.observation import DetectionPayload
from voa.providers.rtdetr import Frame
from voa.server.inference import ModelUnavailable
from voa.server.session import SessionStream
from voa.server.ws import push_messages

if TYPE_CHECKING:
    from voa.server.app import ServerRuntime

JPEG_MAGIC = b"\xff\xd8"
NS_PER_S = 1_000_000_000
LOAD_WINDOW_NS = 5 * NS_PER_S  # average queue_ms over this window (ADR-0010)
RAISE_AFTER_NS = 5 * NS_PER_S  # sustained overload before the interval grows x1.5
RESTORE_AFTER_NS = 10 * NS_PER_S  # sustained calm before it returns to the base
MAX_SIDE = 640
QUALITY_FLAGS = frozenset(get_args(QualityFlag))


@dataclass
class Counters:
    received: int = 0
    processed: int = 0
    skipped: int = 0
    rejected: int = 0


@dataclass
class FrameState:
    """Per-session upload bookkeeping and the load-adaptive frame interval."""

    base_interval_ms: int = 333
    interval_ms: int = 333
    counters: Counters = field(default_factory=Counters)
    samples: deque[tuple[int, float]] = field(default_factory=deque)
    high_since: int | None = None
    low_since: int | None = None
    busy: set[str] = field(default_factory=set)
    last: dict[str, tuple[int, int]] = field(default_factory=dict)  # stream -> (seq, capture)

    def adapt(self, queue_ms: float, now_ns: int, max_interval_ms: int) -> int | None:
        """New interval when the 5 s average queue says so, else None (ADR-0010).

        Average above the interval for 5 s: x1.5 (capped). Below half of it for 10 s:
        back to the base. A longer interval may outgrow `staleness_ms`, and the engine
        then reports `unknown` as it should: load is never hidden.
        """
        self.samples.append((now_ns, queue_ms))
        while self.samples and now_ns - self.samples[0][0] > LOAD_WINDOW_NS:
            self.samples.popleft()
        average = sum(q for _, q in self.samples) / len(self.samples)
        if average > self.interval_ms:
            self.low_since = None
            if self.high_since is None:
                self.high_since = now_ns
            if now_ns - self.high_since >= RAISE_AFTER_NS and self.interval_ms < max_interval_ms:
                self.interval_ms = min(int(self.interval_ms * 1.5), max_interval_ms)
                self.high_since = now_ns
                return self.interval_ms
        elif average < self.interval_ms / 2 and self.interval_ms != self.base_interval_ms:
            self.high_since = None
            if self.low_since is None:
                self.low_since = now_ns
            if now_ns - self.low_since >= RESTORE_AFTER_NS:
                self.interval_ms = self.base_interval_ms
                self.low_since = None
                return self.interval_ms
        else:
            self.high_since = self.low_since = None
        return None

    def snapshot(self) -> FrameCounters:
        c = self.counters
        return FrameCounters(
            received=c.received, processed=c.processed, skipped=c.skipped, rejected=c.rejected
        )


@dataclass(frozen=True)
class FrameHeaders:
    stream_id: str
    seq: int
    frame_id: str
    capture_ts: int
    flags: frozenset[str]


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"code": code}, status_code=status)


def parse_headers(request: Request) -> FrameHeaders | None:
    h = request.headers
    try:
        seq = int(h["x-voa-seq"])
        capture = int(h["x-voa-capture-ts-ns"])
        stream_id = h["x-voa-stream-id"]
        frame_id = h["x-voa-frame-id"]
    except (KeyError, ValueError):
        return None
    flags = frozenset(f.strip() for f in h.get("x-voa-quality-flags", "").split(",") if f.strip())
    if seq < 0 or not stream_id or not frame_id or len(frame_id) > 128 or flags - QUALITY_FLAGS:
        return None
    if not all(c.isalnum() or c in "_.:-" for c in frame_id):
        return None
    return FrameHeaders(stream_id, seq, frame_id, capture, flags)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from the first SOF marker, without decoding the image."""
    if not data.startswith(JPEG_MAGIC):
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        i += 2 + length
    return None


def register_frame_routes(app: FastAPI) -> None:
    @app.post("/v1/sessions/{session_id}/frames")
    async def upload_frame(request: Request, session_id: str) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        received = runtime.mono_ns()
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        owner = (
            runtime.sessions.session_for_access(token, received)
            if scheme.lower() == "bearer" and token
            else None
        )
        if owner is None or owner != session_id:
            return _error(401, "unauthorized")
        state = runtime.frame_states.setdefault(session_id, runtime.new_frame_state())
        counters = state.counters
        counters.received += 1
        stream = runtime.streams.get(session_id)
        if stream is None:
            counters.rejected += 1
            return _error(409, "mission_not_selected")
        if session_id not in runtime.connections:
            counters.rejected += 1
            return _error(409, "control_not_connected")
        headers = parse_headers(request)
        body = await request.body()
        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        size = _jpeg_size(body) if content_type == "image/jpeg" else None
        too_big = len(body) > runtime.settings.frame_max_bytes
        if headers is None or size is None or too_big or max(size) > MAX_SIDE:
            counters.rejected += 1
            return _error(400, "frame_rejected")
        if not stream.accepts_stream(headers.stream_id):
            counters.rejected += 1
            return _error(400, "stream_not_declared")
        last = state.last.get(headers.stream_id)
        if last is not None and (headers.seq <= last[0] or headers.capture_ts < last[1]):
            counters.rejected += 1
            return _error(400, "out_of_order")
        if headers.stream_id in state.busy:
            counters.skipped += 1
            return _error(409, "frame_busy")

        state.busy.add(headers.stream_id)
        state.last[headers.stream_id] = (headers.seq, headers.capture_ts)
        task = asyncio.create_task(_process(runtime, session_id, stream, headers, body, received))
        task.add_done_callback(lambda _: state.busy.discard(headers.stream_id))
        timeout = runtime.settings.frame_sync_timeout_ms / 1000
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            task.add_done_callback(lambda done: _send_later(runtime, session_id, done))
            return JSONResponse({"frame_id": headers.frame_id, "status": "accepted"}, 202)
        except ModelUnavailable:
            return _error(503, "no_model_deployed")
        counters.processed += 1
        await _adapt(runtime, session_id, state, result)
        return JSONResponse(result.model_dump(mode="json"))


def _models(stream: SessionStream) -> list[tuple[str, float]]:
    """(model_id, lowest min_score among scenarios that use it) for detection models."""
    floors: dict[str, float] = {}
    for scenario in stream.profile.scenarios:
        model = scenario.required_models.get("object_detection")
        if model is not None:
            floors[model] = min(floors.get(model, 1.0), stream.profile.params.min_score)
    return sorted(floors.items())


async def _process(
    runtime: "ServerRuntime",
    session_id: str,
    stream: SessionStream,
    headers: FrameHeaders,
    body: bytes,
    received: int,
) -> FrameResult:
    started: int | None = None  # when the first model was free for this frame
    frame = Frame(headers.frame_id, headers.capture_ts, body)
    drafts = []
    provider_ns = 0
    for model_id, floor in _models(stream):

        def run(model_id: str = model_id, floor: float = floor) -> list[object]:
            return list(runtime.models.provider(model_id, floor).process(frame))

        async with runtime.models.lock(model_id):
            if started is None:
                started = runtime.mono_ns()
            begin = time.perf_counter_ns()
            produced = await asyncio.to_thread(run)
            provider_ns += time.perf_counter_ns() - begin
            runtime.models.record(model_id, runtime.mono_ns())
        drafts += [(model_id, d) for d in produced]
    if started is None:
        started = runtime.mono_ns()

    results: list[FrameModelResult] = []
    engine_begin = time.perf_counter_ns()
    if runtime.streams.get(session_id) is stream:  # the mission did not change meanwhile
        for model_id, draft in drafts:
            draft = draft.model_copy(  # type: ignore[attr-defined]
                update={
                    "stream_id": headers.stream_id,
                    "seq": headers.seq,
                    "capture_ts_monotonic_ns": headers.capture_ts,
                    "quality_flags": headers.flags,
                }
            )
            now = runtime.mono_ns()
            observation_id, messages = stream.ingest_with_id(draft, now, received_mono_ns=received)
            websocket = runtime.connections.get(session_id)
            if websocket is not None:
                with contextlib.suppress(RuntimeError, ConnectionError):
                    await push_messages(runtime, stream, websocket, messages)
            payload = draft.payload
            detections = list(payload.detections) if isinstance(payload, DetectionPayload) else []
            results.append(
                FrameModelResult(
                    model_id=model_id, observation_id=observation_id, detections=detections
                )
            )
    engine_us = (time.perf_counter_ns() - engine_begin) // 1000
    completed = runtime.mono_ns()
    return FrameResult(
        frame_id=headers.frame_id,
        seq=headers.seq,
        results=results,
        states=dict(stream.engine.states()),
        timing=FrameTiming(
            capture_ts_monotonic_ns=headers.capture_ts,
            received_at_utc=runtime.clock.at(received),
            processing_started_at_utc=runtime.clock.at(started),
            completed_at_utc=runtime.clock.at(completed),
            queue_ms=round((started - received) / 1e6, 3),
            provider_ms=round(provider_ns / 1e6, 3),
            engine_us=engine_us,
        ),
    )


def _send_later(
    runtime: "ServerRuntime", session_id: str, task: "asyncio.Task[FrameResult]"
) -> None:
    if task.cancelled() or task.exception() is not None:
        return
    state = runtime.frame_states.get(session_id)
    if state is not None:
        state.counters.processed += 1
    websocket = runtime.connections.get(session_id)
    if websocket is None:
        return
    message = FrameResultMessage(**task.result().model_dump())
    asyncio.ensure_future(_send_text(websocket, message.model_dump_json()))
    if state is not None:
        asyncio.ensure_future(_adapt(runtime, session_id, state, task.result()))


async def _adapt(
    runtime: "ServerRuntime", session_id: str, state: FrameState, result: FrameResult
) -> None:
    changed = state.adapt(
        result.timing.queue_ms, runtime.mono_ns(), runtime.settings.frame_interval_max_ms
    )
    websocket = runtime.connections.get(session_id)
    if changed is not None and websocket is not None:
        await _send_text(websocket, ConfigMessage(frame_interval_ms=changed).model_dump_json())


async def _send_text(websocket: object, text: str) -> None:
    with contextlib.suppress(RuntimeError, ConnectionError):
        await websocket.send_text(text)  # type: ignore[attr-defined]
