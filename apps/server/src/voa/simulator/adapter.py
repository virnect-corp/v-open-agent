"""Glass client reference implementation: session, WebSocket, outputs, ACK (SPEC 7.1-7.4).

The client never judges risk. It sends observation Drafts, renders `alert` and
`notice` on its declared outputs, acknowledges alerts, and watches the link:
- ACK: `received` right after parsing and the alert_id duplicate check, `played`
  after at least one output rendered. A repeated alert_id is not rendered again;
  only `played` is re-sent (DEV-02, T-R10).
- Reconnect: REST `refresh` -> WebSocket handshake (`Authorization: Bearer`, ADR-0009)
  -> `alert_sync(last_alert_seq)`
  (ADR-0006). If the server lost the session (refresh 401, e.g. after a restart),
  a new session is created and the alert cursor restarts at 0 (SPEC 7.1).
- Watchdog: `watchdog_ms` without any server message renders the built-in
  disconnect text once; reconnects back off 1/2/4/8 s (DEV-04, T-R16).

Depends only on `voa.contracts` (server JSON Schema), never on `voa.server`.
Tokens and secrets are kept in memory and never logged or printed.
"""

import asyncio
import contextlib
import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from voa.contracts.decision import RiskDecision
from voa.contracts.messages import (
    AckMessage,
    AdmissionMessage,
    AlertMessage,
    AlertSyncMessage,
    AuthOkMessage,
    DecisionMessage,
    ErrorMessage,
    HeartbeatMessage,
    NoticeMessage,
    ObservationMessage,
    ServerEnvelope,
    ServerMessage,
)
from voa.contracts.observation import HealthPayload, ObservationDraft
from voa.providers.base import ObservationProvider
from voa.simulator.fixture import TimedDraft
from voa.simulator.frames import FrameSource
from voa.simulator.outputs import OutputName, VirtualOutputs
from voa.simulator.watchdog import DEFAULT_BACKOFF_S, Backoff, Watchdog

HTTP_TIMEOUT_S = 5.0
AUTH_OK_TIMEOUT_S = 5.0
WATCH_POLL_S = 0.02

HttpPost = Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]


class SessionRejected(Exception):
    """The server refused to create a session. Carries the status and stable code only."""

    def __init__(self, status: int, code: str | None) -> None:
        super().__init__(f"session rejected: {status} {code or ''}".strip())
        self.status = status
        self.code = code


def _percentile_ms(samples: list[int], q: int) -> float | None:
    """Nearest-rank percentile in ms (same rule as voa.replay); None without samples."""
    if not samples:
        return None
    ordered = sorted(samples)
    return round(ordered[max(1, math.ceil(q / 100 * len(ordered))) - 1] / 1e6, 1)


class _LinkDown(Exception):
    """The link failed in a way a reconnect may fix."""


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    # The edge server is local; environment proxies must not see tokens.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Blocking JSON POST. HTTP errors return (status, body); link errors raise OSError."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _no_proxy_opener().open(request, timeout=HTTP_TIMEOUT_S) as response:
            return int(response.status), dict(json.loads(response.read()))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read())
        except ValueError:
            payload = {}
        return int(error.code), payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class FramePlayStats:
    """Stage C0 frame playback counters (ADR-0007). Latency is measurement only."""

    total: int  # decoded frames (corrupt files are counted by FrameSource.skipped)
    sent: int
    late_skipped: int  # the next frame was already due: a newer frame exists
    offline_dropped: int  # processed while the link was down; never queued for later
    corrupt: int
    provider_ms_p50: float | None
    provider_ms_p95: float | None


@dataclass(frozen=True)
class ClientConfig:
    server_url: str  # http(s)://host:port
    device_id: str
    secret: str | None
    inputs: tuple[str, ...]
    outputs: tuple[OutputName, ...]
    backoff_s: Sequence[float] = DEFAULT_BACKOFF_S

    def ws_url(self, session_id: str) -> str:
        base = self.server_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base.removeprefix("https://")
        elif base.startswith("http://"):
            base = "ws://" + base.removeprefix("http://")
        return f"{base}/v1/streams/{session_id}"


@dataclass
class _Credentials:
    session_id: str
    token: str
    refresh_token: str


@dataclass
class GlassClient:
    config: ClientConfig
    mono_ns: Callable[[], int] = time.monotonic_ns
    http_post: HttpPost = post_json
    outputs: VirtualOutputs = field(init=False)
    decisions: list[RiskDecision] = field(default_factory=list, init=False)
    admissions: list[AdmissionMessage] = field(default_factory=list, init=False)
    errors: list[ErrorMessage] = field(default_factory=list, init=False)
    sync_sent: list[tuple[str, int]] = field(default_factory=list, init=False)
    sessions_created: int = field(default=0, init=False)
    last_alert_seq: int = field(default=0, init=False)
    states: dict[str, str] = field(default_factory=dict, init=False)
    connected: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _credentials: _Credentials | None = field(default=None, init=False, repr=False)
    _watchdog: Watchdog | None = field(default=None, init=False)
    _backoff: Backoff = field(init=False)
    _ws: ClientConnection | None = field(default=None, init=False, repr=False)
    _tasks: list["asyncio.Task[None]"] = field(default_factory=list, init=False)
    _health_seq: dict[str, int] = field(default_factory=dict, init=False)
    _restore_pending: bool = field(default=False, init=False)
    _watchdog_announced: bool = field(default=False, init=False)
    _stopping: bool = field(default=False, init=False)
    fatal: SessionRejected | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.outputs = VirtualOutputs(self.config.outputs)
        self._backoff = Backoff(self.config.backoff_s)

    # --- lifecycle ---

    @property
    def session_id(self) -> str | None:
        return None if self._credentials is None else self._credentials.session_id

    async def start(self) -> None:
        """Create the session (errors propagate), then keep the link up in the background."""
        await self._create_session()
        self._tasks = [
            asyncio.create_task(self._link_loop()),
            asyncio.create_task(self._watch_loop()),
        ]

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        await self.drop_connection()

    async def drop_connection(self) -> None:
        """Close the socket as a lost link would; the link loop reconnects."""
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(WebSocketException, OSError):
                await ws.close()

    # --- input ---

    async def send_observation(self, draft: ObservationDraft) -> bool:
        """Send now, or drop when offline: stale frames are never buffered for later."""
        ws = self._ws
        if ws is None:
            return False
        message = ObservationMessage(type="observation", observation=draft)
        try:
            await ws.send(message.model_dump_json())
        except (WebSocketException, OSError):
            return False
        return True

    async def report_health(
        self, stream_id: str, status: Literal["ok", "down"], detail_code: str | None = None
    ) -> bool:
        """DEV-05: sensor error/recovery as a `health` observation for that stream."""
        seq = self._health_seq.get(stream_id, -1) + 1
        self._health_seq[stream_id] = seq
        draft = ObservationDraft(
            stream_id=stream_id,
            seq=seq,
            capture_ts_monotonic_ns=self.mono_ns(),
            payload=HealthPayload(kind="health", status=status, detail_code=detail_code),
        )
        return await self.send_observation(draft)

    async def play(self, rows: Sequence[TimedDraft], *, time_scale: float = 1.0) -> int:
        """Send each Draft at its `at_ms` offset; ticks only advance time. Returns sent count."""
        start = self.mono_ns()
        sent = 0
        for row in rows:
            due_ns = start + int(row.at_ms * time_scale * 1_000_000)
            delay = (due_ns - self.mono_ns()) / 1e9
            if delay > 0:
                await asyncio.sleep(delay)
            if row.draft is not None and await self.send_observation(row.draft):
                sent += 1
        return sent

    async def play_frames(
        self, source: FrameSource, provider: ObservationProvider
    ) -> FramePlayStats:
        """Stage C0: run the detector on each frame at its due time and send the Drafts.

        Decoding and inference run in a worker thread so heartbeats, ACKs and the
        watchdog keep running during CPU inference. A frame is skipped when the
        next frame is already due (inference fell behind); only fresh frames are sent.
        """
        interval_ns = round(1_000_000_000 / source.fps)
        start = self.mono_ns()
        frames = iter(source)
        total = sent = late = offline = 0
        provider_ns: list[int] = []
        while (frame := await asyncio.to_thread(next, frames, None)) is not None:
            total += 1
            due_ns = start + frame.capture_ts_monotonic_ns
            now = self.mono_ns()
            if now >= due_ns + interval_ns:
                late += 1
                continue
            if due_ns > now:
                await asyncio.sleep((due_ns - now) / 1e9)
            started = time.perf_counter_ns()
            drafts = await asyncio.to_thread(provider.process, frame)
            provider_ns.append(time.perf_counter_ns() - started)
            delivered = [await self.send_observation(draft) for draft in drafts]
            if all(delivered):
                sent += 1
            else:
                offline += 1
        return FramePlayStats(
            total=total,
            sent=sent,
            late_skipped=late,
            offline_dropped=offline,
            corrupt=source.skipped,
            provider_ms_p50=_percentile_ms(provider_ns, 50),
            provider_ms_p95=_percentile_ms(provider_ns, 95),
        )

    # --- session ---

    def _session_body(self) -> dict[str, Any]:
        return {
            "device_id": self.config.device_id,
            "secret": self.config.secret,
            "capabilities": {
                "inputs": list(self.config.inputs),
                "outputs": list(self.config.outputs),
            },
        }

    async def _create_session(self) -> None:
        url = f"{self.config.server_url.rstrip('/')}/v1/sessions"
        status, body = await asyncio.to_thread(self.http_post, url, self._session_body())
        if status != 200:
            code = body.get("code")
            raise SessionRejected(status, code if isinstance(code, str) else None)
        self._credentials = _Credentials(
            session_id=str(body["session_id"]),
            token=str(body["token"]),
            refresh_token=str(body["refresh_token"]),
        )
        self.sessions_created += 1
        self.last_alert_seq = 0  # alert_seq is per session (SPEC 7.2)
        watchdog_ms = int(body["watchdog_ms"])
        if self._watchdog is None:
            self._watchdog = Watchdog(watchdog_ms, self.mono_ns())
        else:
            self._watchdog.watchdog_ms = watchdog_ms  # keep the silence measured so far

    async def _refresh_or_recreate(self) -> None:
        creds = self._credentials
        assert creds is not None
        url = f"{self.config.server_url.rstrip('/')}/v1/sessions/{creds.session_id}/refresh"
        status, body = await asyncio.to_thread(
            self.http_post, url, {"refresh_token": creds.refresh_token}
        )
        if status == 200:
            creds.token = str(body["token"])
            creds.refresh_token = str(body["refresh_token"])
            return
        if status != 401:
            raise _LinkDown()
        try:
            await self._create_session()  # server restarted or session swept
        except SessionRejected as error:
            if error.status >= 500:
                raise _LinkDown() from None
            raise

    # --- link ---

    async def _link_loop(self) -> None:
        first = True
        while not self._stopping:
            try:
                if not first:
                    await self._refresh_or_recreate()
                await self._connect_and_serve(reconnect=not first)
            except SessionRejected as error:
                self.fatal = error  # credentials or capabilities refused: stop retrying
                return
            except (_LinkDown, OSError, WebSocketException, TimeoutError):
                pass
            first = False
            if self._stopping:
                return
            await asyncio.sleep(self._backoff.next_delay())

    async def _connect_and_serve(self, *, reconnect: bool) -> None:
        creds = self._credentials
        assert creds is not None
        async with connect(
            self.config.ws_url(creds.session_id),
            proxy=None,
            open_timeout=HTTP_TIMEOUT_S,
            additional_headers={"Authorization": f"Bearer {creds.token}"},  # ADR-0009
        ) as ws:
            first = await asyncio.wait_for(ws.recv(), AUTH_OK_TIMEOUT_S)
            if not isinstance(self._parse(first), AuthOkMessage):
                raise _LinkDown()
            self._feed()
            self._ws = ws
            self.connected.set()
            try:
                await ws.send(
                    AlertSyncMessage(
                        type="alert_sync", last_alert_seq=self.last_alert_seq
                    ).model_dump_json()
                )
                self.sync_sent.append((creds.session_id, self.last_alert_seq))
                self._backoff.reset()
                if reconnect and self._watchdog_announced:
                    self._restore_pending = True
                self._watchdog_announced = False
                async for raw in ws:
                    message = self._parse(raw)
                    self._feed()
                    if message is not None:
                        await self._handle(ws, message)
            finally:
                self._ws = None
                self.connected.clear()

    def _feed(self) -> None:
        if self._watchdog is not None:
            self._watchdog.feed(self.mono_ns())

    async def _watch_loop(self) -> None:
        while not self._stopping:
            watchdog = self._watchdog
            if watchdog is not None and watchdog.should_announce(self.mono_ns()):
                self.outputs.announce_disconnect()
                self._watchdog_announced = True
            await asyncio.sleep(WATCH_POLL_S)

    @staticmethod
    def _parse(raw: str | bytes) -> ServerMessage | None:
        try:
            return ServerEnvelope.model_validate_json(raw).root
        except ValidationError:
            return None  # unknown server messages are ignored, never executed

    async def _handle(self, ws: ClientConnection, message: ServerMessage) -> None:
        if isinstance(message, AlertMessage):
            await self._on_alert(ws, message)
        elif isinstance(message, NoticeMessage):
            self.outputs.render_notice(message)
        elif isinstance(message, HeartbeatMessage):
            self.states = dict(message.states)
            self.outputs.on_heartbeat(message.states, message.server_time_utc)
            if self._restore_pending:
                self._restore_pending = False
                self.outputs.show_restored()
        elif isinstance(message, DecisionMessage):
            self.decisions.append(message.decision)
        elif isinstance(message, AdmissionMessage):
            self.admissions.append(message)
        elif isinstance(message, ErrorMessage):
            self.errors.append(message)

    async def _on_alert(self, ws: ClientConnection, message: AlertMessage) -> None:
        alert_id = message.alert.alert_id
        self.last_alert_seq = max(self.last_alert_seq, message.alert_seq)
        if self.outputs.is_played(alert_id):
            await self._ack(ws, alert_id, "played")  # repeat: no rendering (SPEC 7.4)
            return
        await self._ack(ws, alert_id, "received")
        if self.outputs.render_alert(message):
            await self._ack(ws, alert_id, "played")

    async def _ack(
        self, ws: ClientConnection, alert_id: str, status: Literal["received", "played"]
    ) -> None:
        await ws.send(AckMessage(type="ack", alert_id=alert_id, status=status).model_dump_json())
        self.outputs.record("ack_received" if status == "received" else "ack_played", alert_id)
