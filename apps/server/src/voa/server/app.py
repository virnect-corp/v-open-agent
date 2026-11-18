"""FastAPI composition root (SPEC 7.5, 8.3).

Importing this module never reads settings, files or the environment, and never
starts a server: configuration is loaded in the lifespan of the running app.
"""

import os
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from voa.config.model_registry import check_profile_models, load_manifests
from voa.config.profile_loader import load_profile
from voa.engine.profile import PolicyProfile, ProfileError
from voa.server.auth import (
    CreateSessionRequest,
    IssuedTokens,
    ResumeRequest,
    SessionCreated,
    SessionRegistry,
    TokenPair,
    authenticate_device,
)
from voa.server.devices import DeviceConfig, load_devices
from voa.server.runtime import AnchoredUtcClock, SecureIdFactory
from voa.server.settings import Settings
from voa.storage.sqlite import SQLiteStore

NotReadyCode = Literal["profile_invalid", "db_unavailable", "devices_invalid"]


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
                resume_ttl_s=settings.resume_token_ttl_s,
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

    def close(self) -> None:
        if self.store is not None:
            self.store.close()


def _error(status_code: int, code: str) -> JSONResponse:
    """REST error body is `{"code": ...}` only; request contents are never echoed."""
    return JSONResponse({"code": code}, status_code=status_code)


def _token_pair(runtime: ServerRuntime, tokens: IssuedTokens) -> dict[str, object]:
    return {
        "token": tokens.token,
        "token_expires_at_utc": runtime.clock.at(tokens.token_expires_mono_ns),
        "resume_token": tokens.resume_token,
        "resume_token_expires_at_utc": runtime.clock.at(tokens.resume_expires_mono_ns),
    }


def create_app(
    load_settings: Callable[[], Settings] = settings_from_environment,
    *,
    mono_ns: Callable[[], int] = time.monotonic_ns,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = ServerRuntime.build(load_settings(), mono_ns)
        app.state.runtime = runtime
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

        now = runtime.mono_ns()
        session, tokens = runtime.sessions.create(body.device_id, caps, now)
        if runtime.store is not None:
            runtime.store.save_session(
                session_id=session.session_id,
                device_id=session.device_id,
                started_at_utc=runtime.clock.at(now),
            )
        created = SessionCreated.model_validate(
            {
                "session_id": session.session_id,
                **_token_pair(runtime, tokens),
                "heartbeat_ms": runtime.settings.heartbeat_ms,
                "watchdog_ms": runtime.settings.client_watchdog_ms,
            }
        )
        return JSONResponse(created.model_dump(mode="json"))

    @app.post("/v1/sessions/{session_id}/resume")
    async def resume_session(
        request: Request, session_id: str, body: ResumeRequest
    ) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        tokens = runtime.sessions.resume(session_id, body.resume_token, runtime.mono_ns())
        if tokens is None:
            return _error(401, "unauthorized")
        pair = TokenPair.model_validate(_token_pair(runtime, tokens))
        return JSONResponse(pair.model_dump(mode="json"))

    return app


app = create_app()
