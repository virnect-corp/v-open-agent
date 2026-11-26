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
from voa.server.accounts import (
    ADMIN_USERNAME,
    Accounts,
    LoginRequest,
    LoginResponse,
    LoginToken,
    LoginTokens,
    PasswordChangeRequest,
    Role,
    SignupRequest,
    public_user,
)
from voa.server.auth import (
    ALL_INPUTS,
    ALL_OUTPUTS,
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
from voa.server.ws import CLOSE_UNAUTHORIZED, input_loop, register_stream_routes, tick_loop
from voa.storage.sqlite import SQLiteStore

NotReadyCode = Literal["profile_invalid", "db_unavailable", "devices_invalid"]
RETENTION_PURGE_INTERVAL_S = 3600
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


class AdminDefaultPasswordError(RuntimeError):
    """The port is network-exposed while `admin` still has the default password (ADR-0011)."""


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
    login_tokens: LoginTokens
    accounts: Accounts | None = None
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
            login_tokens=LoginTokens(ttl_ns=settings.token_ttl_s * 1_000_000_000),
            accounts=Accounts(store) if store is not None else None,
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
        auth = self.sessions.get(session_id)
        self.sessions.remove(session_id)
        if stream is not None and self.store is not None:
            self.store.save_session(
                session_id=session_id,
                device_id=stream.device_id,
                started_at_utc=stream.started_at_utc,
                ended_at_utc=self.clock.at(now_mono_ns),
                user_id=auth.user_id if auth is not None else None,
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


def _bearer(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token if scheme.lower() == "bearer" and token else None


def _login(runtime: "ServerRuntime", request: Request) -> LoginToken | None:
    token = _bearer(request)
    return runtime.login_tokens.lookup(token, runtime.mono_ns()) if token else None


def _from_loopback(runtime: "ServerRuntime", request: Request) -> bool:
    """Loopback peer, or a port the operator declared loopback-only (compose)."""
    host = request.client.host if request.client is not None else ""
    return runtime.settings.exposure == "loopback" or host in LOOPBACK_HOSTS


def _admin_guard(runtime: "ServerRuntime", request: Request) -> JSONResponse | None:
    """401 without an admin login, 403 until the default password is changed."""
    login = _login(runtime, request)
    if login is None or login.role != "admin" or runtime.accounts is None:
        return _error(401, "unauthorized")
    user = runtime.store.get_user(login.user_id) if runtime.store is not None else None
    if user is None:
        return _error(401, "unauthorized")
    if user["password_change_required"]:
        return _error(403, "password_change_required")
    return None


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
        if runtime.accounts is not None:
            runtime.accounts.ensure_admin(runtime.clock.at(runtime.mono_ns()))
            exposed = runtime.settings.exposure == "network"
            if exposed and runtime.accounts.admin_has_default_password():
                runtime.close()
                raise AdminDefaultPasswordError("change the admin password before exposing")
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
        user_id: str | None = None
        if body.username is not None:  # wearer account (ADR-0011)
            if body.device_id is not None or runtime.accounts is None:
                return _error(400, "invalid_request")
            result, user = runtime.accounts.authenticate(
                body.username, body.password or "", runtime.mono_ns()
            )
            if result == "locked":
                return _error(429, "too_many_attempts")
            if result == "pending":
                return _error(403, "account_pending")
            if result != "ok" or user is None or user["role"] != "wearer":
                return _error(401, "unauthorized")
            if user["password_change_required"]:
                return _error(403, "password_change_required")
            user_id = str(user["user_id"])
            allowed: tuple[frozenset[str], frozenset[str]] | None = (ALL_INPUTS, ALL_OUTPUTS)
        elif body.device_id is None:
            return _error(400, "invalid_request")
        else:
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
        device_id = body.device_id if user_id is None else user_id
        assert device_id is not None
        session, tokens = runtime.sessions.create(device_id, caps, now, user_id=user_id)
        if runtime.store is not None:
            runtime.store.save_session(
                session_id=session.session_id,
                device_id=session.device_id,
                started_at_utc=runtime.clock.at(now),
                user_id=user_id,
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

    # --- Accounts (ADR-0011, SPEC 7.1) ---

    @app.post("/v1/auth/login")
    async def login(request: Request, body: LoginRequest) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        if runtime.accounts is None:
            return _error(503, "not_ready")
        now = runtime.mono_ns()
        result, user = runtime.accounts.authenticate(body.username, body.password, now)
        if result == "locked":
            return _error(429, "too_many_attempts")
        if result == "pending":
            return _error(403, "account_pending")
        if result != "ok" or user is None:
            return _error(401, "unauthorized")
        default_admin = body.username == ADMIN_USERNAME and bool(user["password_change_required"])
        if default_admin and not _from_loopback(runtime, request):
            return _error(401, "unauthorized")  # the default password is loopback-only
        role: Role = "admin" if user["role"] == "admin" else "wearer"
        token, expires = runtime.login_tokens.issue(str(user["user_id"]), role, now)
        body_out = LoginResponse(
            token=token,
            token_expires_at_utc=runtime.clock.at(expires),
            role=role,
            password_change_required=bool(user["password_change_required"]),
        )
        return JSONResponse(body_out.model_dump(mode="json"))

    @app.post("/v1/auth/signup")
    async def signup(request: Request, body: SignupRequest) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        if runtime.accounts is None:
            return _error(503, "not_ready")
        if not runtime.settings.signup_enabled:
            return _error(403, "signup_disabled")
        result = runtime.accounts.signup(
            body,
            requires_approval=runtime.settings.signup_requires_approval,
            now_utc=runtime.clock.at(runtime.mono_ns()),
        )
        if result == "invalid":
            return _error(400, "invalid_request")
        if result == "taken":
            return _error(409, "username_taken")
        user_id, status = result
        return JSONResponse({"user_id": user_id, "status": status}, status_code=201)

    @app.post("/v1/auth/password")
    async def change_password(request: Request, body: PasswordChangeRequest) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        login = _login(runtime, request)
        if login is None or runtime.accounts is None:
            return _error(401, "unauthorized")
        changed = runtime.accounts.change_password(
            login.user_id,
            body.current_password,
            body.new_password,
            runtime.clock.at(runtime.mono_ns()),
        )
        if not changed:
            return _error(400, "invalid_request")
        return JSONResponse({"password_change_required": False})

    @app.get("/v1/admin/users")
    async def list_users(request: Request) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        assert runtime.store is not None
        return JSONResponse({"users": [public_user(u) for u in runtime.store.list_users()]})

    @app.post("/v1/admin/users/{user_id}:approve")
    async def approve_user(request: Request, user_id: str) -> JSONResponse:
        return await _set_status(request, user_id, "active")

    @app.post("/v1/admin/users/{user_id}:disable")
    async def disable_user(request: Request, user_id: str) -> JSONResponse:
        return await _set_status(request, user_id, "disabled")

    @app.post("/v1/admin/users/{user_id}:reset-password")
    async def reset_password(request: Request, user_id: str) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        assert runtime.accounts is not None
        temporary = runtime.accounts.reset_password(user_id, runtime.clock.at(runtime.mono_ns()))
        if temporary is None:
            return _error(404, "not_found")
        runtime.login_tokens.revoke_user(user_id)
        return JSONResponse({"user_id": user_id, "temporary_password": temporary})

    register_stream_routes(app)
    return app


async def _set_status(
    request: Request, user_id: str, status: Literal["active", "disabled"]
) -> JSONResponse:
    runtime: ServerRuntime = request.app.state.runtime
    denied = _admin_guard(runtime, request)
    if denied is not None:
        return denied
    assert runtime.accounts is not None
    now = runtime.mono_ns()
    user = runtime.accounts.set_status(user_id, status, runtime.clock.at(now))
    if user is None:
        return _error(404, "not_found")
    if status == "disabled":  # ADR-0011: running sessions end, logins stop working
        for session_id in runtime.sessions.for_user(user_id):
            websocket = runtime.connections.pop(session_id, None)
            if websocket is not None:
                with contextlib.suppress(RuntimeError):
                    await websocket.close(code=CLOSE_UNAUTHORIZED)
            runtime.end_session(session_id, now)
        runtime.login_tokens.revoke_user(user_id)
    return JSONResponse(public_user(user))


app = create_app()
