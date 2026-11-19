"""FastAPI composition root (SPEC 7.5, 8.3).

Importing this module never reads settings, files or the environment, and never
starts a server: configuration is loaded in the lifespan of the running app.
"""

import asyncio
import contextlib
import os
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from voa.config.model_registry import check_profile_models, load_manifests
from voa.config.profile_loader import load_profile
from voa.engine.profile import PolicyProfile, ProfileError
from voa.server.auth import (
    AckRequest,
    CreateSessionRequest,
    IssuedTokens,
    RefreshRequest,
    SessionCreated,
    SessionRegistry,
    TokenPair,
    authenticate_device,
)
from voa.server.devices import DeviceConfig, load_devices
from voa.server.logconfig import pin_log_levels
from voa.server.runtime import AnchoredUtcClock, SecureIdFactory
from voa.server.session import SessionStream
from voa.server.settings import Settings
from voa.server.ws import input_loop, register_stream_routes, tick_loop
from voa.storage.sqlite import SQLiteStore

NotReadyCode = Literal["profile_invalid", "db_unavailable", "devices_invalid"]
RETENTION_PURGE_INTERVAL_S = 3600


def settings_from_environment() -> Settings:
    """Relative paths resolve against the working directory (apps/server, SPEC 8.3)."""
    return Settings.from_env(os.environ, base_dir=Path.cwd())


def load_policy(settings: Settings) -> PolicyProfile | ProfileError:
    """Profile file plus model manifest cross-check (SPEC 4.9, ADR-0004)."""
    profile = load_profile(settings.policy_profile)
    if isinstance(profile, ProfileError):
        return profile
    manifests = load_manifests(settings.models_dir)
    if isinstance(manifests, ProfileError):
        return manifests
    return check_profile_models(profile, manifests) or profile


@dataclass
class ServerRuntime:
    """Process-wide state shared by all sessions."""

    settings: Settings
    profile: PolicyProfile | ProfileError
    store: SQLiteStore | None
    devices: dict[str, DeviceConfig] | None
    ids: SecureIdFactory
    clock: AnchoredUtcClock
    mono_ns: Callable[[], int]
    sessions: SessionRegistry
    streams: dict[str, SessionStream] = field(default_factory=dict)
    connections: dict[str, WebSocket] = field(default_factory=dict)
    tick_tasks: dict[str, "asyncio.Task[None]"] = field(default_factory=dict)
    input_tasks: dict[str, "asyncio.Task[None]"] = field(default_factory=dict)
    background: list["asyncio.Task[None]"] = field(default_factory=list)

    @classmethod
    def build(
        cls, settings: Settings, mono_ns: Callable[[], int] = time.monotonic_ns
    ) -> "ServerRuntime":
        try:
            store: SQLiteStore | None = SQLiteStore(settings.db_path)
        except (sqlite3.Error, OSError):
            store = None
        return cls(
            settings=settings,
            profile=load_policy(settings),
            store=store,
            devices=load_devices(settings.devices_file),
            ids=(ids := SecureIdFactory()),
            clock=AnchoredUtcClock(datetime.now(UTC), mono_ns()),
            mono_ns=mono_ns,
            sessions=SessionRegistry(
                ids,
                token_ttl_s=settings.token_ttl_s,
                refresh_ttl_s=settings.refresh_token_ttl_s,
            ),
        )

    def not_ready_code(self) -> NotReadyCode | None:
        """First failing check in SPEC order: profile, DB, devices."""
        if isinstance(self.profile, ProfileError):
            return "profile_invalid"
        if not self._db_available():
            return "db_unavailable"
        if self.devices is None:
            return "devices_invalid"
        return None

    def _db_available(self) -> bool:
        if self.store is None:
            return False
        try:
            self.store.get_session("readyz-probe")
        except sqlite3.Error:
            return False
        return True

    def end_session(self, session_id: str, now_mono_ns: int) -> None:
        """Stop the tick loop, forget engine and token digests, record ended_at_utc."""
        for tasks in (self.tick_tasks, self.input_tasks):
            task = tasks.pop(session_id, None)
            if task is not None:
                task.cancel()
        stream = self.streams.pop(session_id, None)
        self.sessions.remove(session_id)
        if stream is not None and self.store is not None:
            self.store.save_session(
                session_id=session_id,
                device_id=stream.device_id,
                started_at_utc=stream.started_at_utc,
                ended_at_utc=self.clock.at(now_mono_ns),
            )

    def sweep_sessions(self, now_mono_ns: int) -> list[str]:
        """ADR-0005: end sessions that are disconnected and can no longer be refreshed."""
        ended = [
            session_id
            for session_id in self.sessions.refresh_expired(now_mono_ns)
            if session_id not in self.connections
        ]
        for session_id in ended:
            self.end_session(session_id, now_mono_ns)
        return ended

    def purge_metadata(self) -> None:
        """SPEC 8.1: delete rows older than VOA_METADATA_RETENTION_DAYS."""
        if self.store is None:
            return
        with contextlib.suppress(sqlite3.Error):
            self.store.purge_expired(
                now_utc=self.clock.at(self.mono_ns()),
                retention_days=self.settings.metadata_retention_days,
            )

    def close(self) -> None:
        for task in [*self.tick_tasks.values(), *self.input_tasks.values(), *self.background]:
            task.cancel()
        if self.store is not None:
            self.store.close()


def _bearer_session(runtime: "ServerRuntime", request: Request, now_mono_ns: int) -> str | None:
    """`Authorization: Bearer <access token>` -> session ID (SPEC 7.1)."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return runtime.sessions.session_for_access(token, now_mono_ns)


async def _every(interval_s: float, action: Callable[[], object]) -> None:
    while True:
        await asyncio.sleep(interval_s)
        action()


def _error(status_code: int, code: str) -> JSONResponse:
    """REST error body is `{"code": ...}` only; request contents are never echoed."""
    return JSONResponse({"code": code}, status_code=status_code)


def _token_pair(runtime: ServerRuntime, tokens: IssuedTokens) -> dict[str, object]:
    return {
        "token": tokens.token,
        "token_expires_at_utc": runtime.clock.at(tokens.token_expires_mono_ns),
        "refresh_token": tokens.refresh_token,
        "refresh_token_expires_at_utc": runtime.clock.at(tokens.refresh_expires_mono_ns),
    }


def create_app(
    load_settings: Callable[[], Settings] = settings_from_environment,
    *,
    mono_ns: Callable[[], int] = time.monotonic_ns,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pin_log_levels()  # no DEBUG frame dumps, however uvicorn was started (DAT-02)
        runtime = ServerRuntime.build(load_settings(), mono_ns)
        app.state.runtime = runtime
        runtime.purge_metadata()  # at start, then hourly (SPEC 8.1)
        runtime.background += [
            asyncio.create_task(_every(RETENTION_PURGE_INTERVAL_S, runtime.purge_metadata)),
            asyncio.create_task(
                _every(
                    runtime.settings.session_sweep_interval_s,
                    lambda: runtime.sweep_sessions(runtime.mono_ns()),
                )
            ),
        ]
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title="v-open-agent", lifespan=lifespan)

    # Handlers are async so SQLite is used on the event-loop thread that opened it.
    @app.get("/v1/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/readyz")
    async def readyz(request: Request) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        code = runtime.not_ready_code()
        if code is None:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready", "error_code": code}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 echoes the input, which may contain a secret.
        return _error(400, "invalid_request")

    @app.post("/v1/sessions")
    async def create_session(request: Request, body: CreateSessionRequest) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        if runtime.not_ready_code() is not None:
            return _error(503, "not_ready")
        allowed = authenticate_device(
            body, runtime.devices, allow_insecure_dev=runtime.settings.allow_insecure_dev
        )
        if allowed is None:
            return _error(401, "unauthorized")
        inputs, outputs = allowed
        caps = body.capabilities
        if not set(caps.inputs) <= inputs or not set(caps.outputs) <= outputs:
            return _error(400, "capability_not_allowed")
        if not caps.outputs:
            return _error(400, "no_output_capability")

        profile = runtime.profile
        assert isinstance(profile, PolicyProfile)  # guaranteed by the readiness check
        now = runtime.mono_ns()
        session, tokens = runtime.sessions.create(body.device_id, caps, now)
        if runtime.store is not None:
            runtime.store.save_session(
                session_id=session.session_id,
                device_id=session.device_id,
                started_at_utc=runtime.clock.at(now),
            )
        stream = SessionStream.start(
            session_id=session.session_id,
            device_id=session.device_id,
            inputs=frozenset(caps.inputs),
            profile=profile,
            ids=runtime.ids,
            clock=runtime.clock,
            store=runtime.store,
            now_mono_ns=now,
            input_queue_max=runtime.settings.input_queue_max,
        )
        runtime.streams[session.session_id] = stream
        runtime.tick_tasks[session.session_id] = asyncio.create_task(tick_loop(runtime, stream))
        runtime.input_tasks[session.session_id] = asyncio.create_task(input_loop(runtime, stream))
        created = SessionCreated.model_validate(
            {
                "session_id": session.session_id,
                **_token_pair(runtime, tokens),
                "heartbeat_ms": runtime.settings.heartbeat_ms,
                "watchdog_ms": runtime.settings.client_watchdog_ms,
            }
        )
        return JSONResponse(created.model_dump(mode="json"))

    @app.post("/v1/sessions/{session_id}/refresh")
    async def refresh_session(
        request: Request, session_id: str, body: RefreshRequest
    ) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        tokens = runtime.sessions.refresh(session_id, body.refresh_token, runtime.mono_ns())
        if tokens is None:
            return _error(401, "unauthorized")
        pair = TokenPair.model_validate(_token_pair(runtime, tokens))
        return JSONResponse(pair.model_dump(mode="json"))

    @app.get("/v1/sessions/{session_id}/status")
    async def session_status(request: Request, session_id: str) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        now = runtime.mono_ns()
        owner = _bearer_session(runtime, request, now)
        if owner is None:
            return _error(401, "unauthorized")
        stream = runtime.streams.get(session_id)
        if owner != session_id or stream is None:
            return _error(404, "not_found")  # another device's session looks missing
        return JSONResponse(stream.status(now))

    @app.post("/v1/alerts/{alert_id}/ack")
    async def ack_alert(request: Request, alert_id: str, body: AckRequest) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        now = runtime.mono_ns()
        session_id = _bearer_session(runtime, request, now)
        if session_id is None:
            return _error(401, "unauthorized")
        stream = runtime.streams.get(session_id)
        if stream is None or alert_id not in stream.dispatcher.records:
            return _error(404, "not_found")  # another session's alert looks the same
        stream.ack(alert_id, body.status, now)
        state = stream.dispatcher.records[alert_id].state
        return JSONResponse({"alert_id": alert_id, "delivery_state": state})

    register_stream_routes(app)
    return app


app = create_app()
