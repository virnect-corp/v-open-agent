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
from fastapi.responses import JSONResponse, Response

from voa.config.missions import MissionStore, MissionVersion
from voa.config.model_registry import check_profile_models, load_manifests
from voa.config.profile_loader import load_profile
from voa.contracts.messages import ConfigMessage, MissionMessage, Presentation
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
from voa.server.admin import register_admin_routes
from voa.server.auth import (
    ALL_INPUTS,
    ALL_OUTPUTS,
    AckRequest,
    CreateSessionRequest,
    IssuedTokens,
    MissionRef,
    RefreshRequest,
    SelectMissionRequest,
    SessionCreated,
    SessionRegistry,
    TokenPair,
    authenticate_device,
)
from voa.server.devices import DeviceConfig, load_devices
from voa.server.frames import FrameState, register_frame_routes
from voa.server.hud import (
    ATTENTION_DEFAULT,
    DEGRADED_DEFAULT,
    NOTICE_KEY,
    AdminSettings,
    SettingsStore,
    choose_style,
    present,
)
from voa.server.inference import DetectorFactory, ModelPool, ModelUnavailable, default_factory
from voa.server.logconfig import pin_log_levels
from voa.server.runtime import AnchoredUtcClock, SecureIdFactory
from voa.server.session import SessionStream
from voa.server.settings import Settings
from voa.server.ui import register_ui_routes
from voa.server.ws import CLOSE_UNAUTHORIZED, input_loop, register_stream_routes, tick_loop
from voa.storage.sqlite import SQLiteStore

NotReadyCode = Literal["profile_invalid", "db_unavailable", "devices_invalid"]
RETENTION_PURGE_INTERVAL_S = 3600
CAPACITY_RETRY_AFTER_S = 30  # ADR-0010
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


def default_mission(
    settings: Settings, missions: MissionStore
) -> tuple[str | None, MissionVersion | ProfileError | None]:
    """ADR-0010: profiles become missions v1 once; pick the default mission.

    `VOA_DEFAULT_MISSION` empty = the mission of `VOA_POLICY_PROFILE` (the pre-mission
    behaviour), `-` = none until the device picks one.
    """
    missions.import_profiles(settings.policy_profile.parent, first=settings.policy_profile)
    if settings.default_mission == "-":
        return None, None
    mission_id = settings.default_mission
    if not mission_id:
        profile = load_policy(settings)
        if isinstance(profile, ProfileError):
            return None, profile
        mission_id = profile.profile_id
    found = missions.published(mission_id)
    if found is None:
        return mission_id, ProfileError("file_not_found", f"mission:{mission_id}")
    return mission_id, found


@dataclass
class ServerRuntime:
    """Process-wide state shared by all sessions."""

    settings: Settings
    profile: PolicyProfile | ProfileError | None  # the default mission's, None = no default
    store: SQLiteStore | None
    devices: dict[str, DeviceConfig] | None
    ids: SecureIdFactory
    clock: AnchoredUtcClock
    mono_ns: Callable[[], int]
    sessions: SessionRegistry
    login_tokens: LoginTokens
    accounts: Accounts | None = None
    missions: MissionStore | None = None
    default_mission: MissionVersion | None = None
    session_missions: dict[str, MissionVersion] = field(default_factory=dict)
    session_started: dict[str, tuple[str, datetime, str | None]] = field(default_factory=dict)
    # TASK-47: when each session lost (or never had) its control socket
    idle_since: dict[str, int] = field(default_factory=dict)
    models: ModelPool = field(default_factory=lambda: ModelPool({}, Path(".")))
    frame_states: dict[str, FrameState] = field(default_factory=dict)
    admin_settings: AdminSettings | None = None  # None = the defaults from VOA_* settings
    settings_valid: bool = True
    streams: dict[str, SessionStream] = field(default_factory=dict)
    connections: dict[str, WebSocket] = field(default_factory=dict)
    tick_tasks: dict[str, "asyncio.Task[None]"] = field(default_factory=dict)
    input_tasks: dict[str, "asyncio.Task[None]"] = field(default_factory=dict)
    background: list["asyncio.Task[None]"] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        settings: Settings,
        mono_ns: Callable[[], int] = time.monotonic_ns,
        detector_factory: DetectorFactory = default_factory,
    ) -> "ServerRuntime":
        try:
            store: SQLiteStore | None = SQLiteStore(settings.db_path)
        except (sqlite3.Error, OSError):
            store = None
        missions = MissionStore(settings.active_config_dir, settings.models_dir)
        try:
            _, mission = default_mission(settings, missions)
        except OSError:
            mission = ProfileError("file_not_found", "missions")
        profile: PolicyProfile | ProfileError | None = (
            mission.profile if isinstance(mission, MissionVersion) else mission
        )
        manifests = load_manifests(settings.models_dir)
        pool = ModelPool(
            manifests if isinstance(manifests, dict) else {},
            settings.model_weights_dir,
            detector_factory,
        )
        settings_store = SettingsStore(settings.active_config_dir, settings.frame_interval_ms)
        admin_settings, settings_valid = settings_store.load()
        return cls(
            settings=settings,
            profile=profile,
            models=pool,
            admin_settings=admin_settings,
            settings_valid=settings_valid,
            missions=missions,
            default_mission=mission if isinstance(mission, MissionVersion) else None,
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

    def start_mission(
        self, session_id: str, mission: MissionVersion, inputs: frozenset[str], now_mono_ns: int
    ) -> SessionStream:
        """A new engine for the session's mission; a previous one is discarded (ADR-0010)."""
        self._stop_stream(session_id)
        device_id = self.session_started[session_id][0]
        stream = SessionStream.start(
            session_id=session_id,
            device_id=device_id,
            inputs=inputs,
            profile=mission.profile,
            ids=self.ids,
            clock=self.clock,
            store=self.store,
            now_mono_ns=now_mono_ns,
            input_queue_max=self.settings.input_queue_max,
        )
        stream.presenter = lambda kind, scenario_id, text: self.presentation(
            mission, kind, scenario_id, text
        )
        self.streams[session_id] = stream
        self.session_missions[session_id] = mission
        if session_id in self.frame_states:  # a new mission brings its own frame interval
            self.frame_states[session_id] = self.new_frame_state(session_id)
        self.tick_tasks[session_id] = asyncio.create_task(tick_loop(self, stream))
        self.input_tasks[session_id] = asyncio.create_task(input_loop(self, stream))
        self._warm_up(mission)
        return stream

    @property
    def current_settings(self) -> AdminSettings:
        if self.admin_settings is None:
            return AdminSettings(frame_interval_ms=self.settings.frame_interval_ms)
        return self.admin_settings

    def interval_for(self, mission: MissionVersion | None) -> int:
        """The mission's recommended frame interval, else the admin setting (ADR-0010)."""
        if mission is not None and mission.settings.frame_interval_ms is not None:
            return mission.settings.frame_interval_ms
        return self.current_settings.frame_interval_ms

    def presentation(
        self, mission: MissionVersion, kind: str, scenario_id: str, text: str
    ) -> Presentation:
        """SPEC 7.4: mission map, then admin map (scenario > severity), then the default.

        Read at send time, so a settings change applies from the next alert.
        """
        settings = self.current_settings
        styles = settings.styles()
        if kind == "notice":
            keys: tuple[str, ...] = (NOTICE_KEY,)
            default = DEGRADED_DEFAULT
        else:
            severity = next(
                (s.severity for s in mission.profile.scenarios if s.id == scenario_id), ""
            )
            keys = (scenario_id, severity)
            default = ATTENTION_DEFAULT
        style_id = choose_style(
            keys, mission.settings.hud_style_map, settings.hud_style_map, styles, default
        )
        return present(style_id, styles, text)

    async def apply_settings(self, settings: AdminSettings) -> None:
        """Adopt new admin settings; tell sessions whose frame interval changed (`config`)."""
        before = {sid: self.interval_for(m) for sid, m in self.session_missions.items()}
        self.admin_settings = settings
        self.settings_valid = True
        for session_id, mission in list(self.session_missions.items()):
            interval = self.interval_for(mission)
            if interval == before.get(session_id):
                continue
            if session_id in self.frame_states:
                self.frame_states[session_id] = self.new_frame_state(session_id)
            websocket = self.connections.get(session_id)
            if websocket is not None:
                with contextlib.suppress(RuntimeError, ConnectionError):
                    await websocket.send_text(
                        ConfigMessage(frame_interval_ms=interval).model_dump_json()
                    )

    def new_frame_state(self, session_id: str) -> FrameState:
        base = self.interval_for(self.session_missions.get(session_id))
        return FrameState(base_interval_ms=base, interval_ms=base)

    def current_default(self) -> MissionVersion | None:
        """The default mission's latest published version (an admin may have published since)."""
        if self.default_mission is None or self.missions is None:
            return None
        latest = self.missions.published(self.default_mission.info.mission_id)
        return latest if isinstance(latest, MissionVersion) else None

    def mission_users(self, mission_id: str) -> int:
        return sum(1 for m in self.session_missions.values() if m.info.mission_id == mission_id)

    def capacity_body(self) -> dict[str, object]:
        return {
            "code": "capacity_exceeded",
            "active": self.sessions.count(),
            "max": self.settings.max_sessions,
            "retry_after_s": CAPACITY_RETRY_AFTER_S,
        }

    def _warm_up(self, mission: MissionVersion) -> None:
        """Load the mission's detectors in the background so the first frame is not slow."""
        for scenario in mission.profile.scenarios:
            model_id = scenario.required_models.get("object_detection")
            if model_id is not None and model_id in self.models.manifests:
                task = asyncio.create_task(asyncio.to_thread(self._try_load, model_id))
                self.background.append(task)

    def _try_load(self, model_id: str) -> None:
        with contextlib.suppress(ModelUnavailable):
            self.models.detector(model_id)

    def mission_message(self, session_id: str) -> MissionMessage | None:
        mission = self.session_missions.get(session_id)
        if mission is None:
            return None
        return MissionMessage(
            mission_id=mission.info.mission_id,
            version=mission.version,
            name=mission.info.name,
            frame_interval_ms=self.interval_for(mission),
        )

    def _stop_stream(self, session_id: str) -> None:
        for tasks in (self.tick_tasks, self.input_tasks):
            task = tasks.pop(session_id, None)
            if task is not None:
                task.cancel()
        self.streams.pop(session_id, None)

    def end_session(self, session_id: str, now_mono_ns: int) -> None:
        """Stop the loops, forget engine and token digests, record ended_at_utc."""
        self._stop_stream(session_id)
        self.session_missions.pop(session_id, None)
        self.frame_states.pop(session_id, None)
        self.idle_since.pop(session_id, None)
        started = self.session_started.pop(session_id, None)
        self.sessions.remove(session_id)
        if started is not None and self.store is not None:
            device_id, started_at, user_id = started
            self.store.save_session(
                session_id=session_id,
                device_id=device_id,
                started_at_utc=started_at,
                ended_at_utc=self.clock.at(now_mono_ns),
                user_id=user_id,
            )

    def sweep_sessions(self, now_mono_ns: int) -> list[str]:
        """End sessions without a control socket that can no longer be refreshed (ADR-0005)
        or have had none for `VOA_SESSION_IDLE_S` (TASK-47: a crashed app frees its slot)."""
        idle_ns = self.settings.session_idle_s * 1_000_000_000
        expired = set(self.sessions.refresh_expired(now_mono_ns))
        expired |= {
            session_id
            for session_id, since in self.idle_since.items()
            if now_mono_ns - since >= idle_ns
        }
        ended = sorted(session_id for session_id in expired if session_id not in self.connections)
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


def _mission_ref(runtime: "ServerRuntime", mission: MissionVersion | None) -> MissionRef | None:
    if mission is None:
        return None
    return MissionRef(
        mission_id=mission.info.mission_id,
        version=mission.version,
        name=mission.info.name,
        frame_interval_ms=runtime.interval_for(mission),
    )


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
    detector_factory: DetectorFactory = default_factory,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pin_log_levels()  # no DEBUG frame dumps, however uvicorn was started (DAT-02)
        runtime = ServerRuntime.build(load_settings(), mono_ns, detector_factory)
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
        runtime.sweep_sessions(runtime.mono_ns())  # free idle slots before counting (TASK-47)
        if runtime.sessions.count() >= runtime.settings.max_sessions:  # ADR-0010
            return JSONResponse(
                runtime.capacity_body(),
                status_code=503,
                headers={"Retry-After": str(CAPACITY_RETRY_AFTER_S)},
            )
        user_id: str | None = None
        if body.username is not None:  # wearer, or admin on the sim page (ADR-0011, TASK-46A)
            if body.device_id is not None or runtime.accounts is None:
                return _error(400, "invalid_request")
            result, user = runtime.accounts.authenticate(
                body.username, body.password or "", runtime.mono_ns()
            )
            if result == "locked":
                return _error(429, "too_many_attempts")
            if result == "pending":
                return _error(403, "account_pending")
            if result != "ok" or user is None or user["role"] not in ("wearer", "admin"):
                return _error(401, "unauthorized")
            if user["password_change_required"]:
                return _error(403, "password_change_required")
            user_id = str(user["user_id"])
            allowed: tuple[frozenset[str], frozenset[str]] | None = (ALL_INPUTS, ALL_OUTPUTS)
        elif body.device_id is None:
            # A logged-in account connects (TASK-47; TASK-46A for admins):
            # `Authorization: Bearer <login token>` and no credentials in the body.
            if _bearer(request) is None:
                return _error(400, "invalid_request")
            login = _login(runtime, request)
            if login is None or runtime.store is None:
                return _error(401, "unauthorized")
            account = runtime.store.get_user(login.user_id)
            if account is None or account["status"] != "active":
                return _error(401, "unauthorized")
            if account["password_change_required"]:
                return _error(403, "password_change_required")
            user_id = login.user_id
            allowed = (ALL_INPUTS, ALL_OUTPUTS)
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

        mission = runtime.current_default()
        if body.mission_id is not None:
            chosen = runtime.missions.published(body.mission_id) if runtime.missions else None
            if not isinstance(chosen, MissionVersion):
                return _error(404, "mission_not_found")
            mission = chosen
        now = runtime.mono_ns()
        device_id = body.device_id if user_id is None else user_id
        assert device_id is not None
        session, tokens = runtime.sessions.create(device_id, caps, now, user_id=user_id)
        started_at = runtime.clock.at(now)
        runtime.session_started[session.session_id] = (session.device_id, started_at, user_id)
        runtime.idle_since[session.session_id] = now  # until its control socket connects
        if runtime.store is not None:
            runtime.store.save_session(
                session_id=session.session_id,
                device_id=session.device_id,
                started_at_utc=started_at,
                user_id=user_id,
            )
        if mission is not None:
            runtime.start_mission(session.session_id, mission, frozenset(caps.inputs), now)
        created = SessionCreated.model_validate(
            {
                "session_id": session.session_id,
                **_token_pair(runtime, tokens),
                "heartbeat_ms": runtime.settings.heartbeat_ms,
                "watchdog_ms": runtime.settings.client_watchdog_ms,
                "frame_interval_ms": runtime.interval_for(mission),
                "mission": _mission_ref(runtime, mission),
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

    @app.delete("/v1/sessions/{session_id}")
    async def disconnect_session(request: Request, session_id: str) -> Response:
        """Disconnect (TASK-47): the device leaves detection and gives its slot back.

        The session token is required; the control socket closes with 1000 and the
        session ends (ended_at_utc is recorded). The login stays valid.
        """
        runtime: ServerRuntime = request.app.state.runtime
        now = runtime.mono_ns()
        owner = _bearer_session(runtime, request, now)
        if owner is None:
            return _error(401, "unauthorized")
        if owner != session_id:
            return _error(404, "not_found")
        websocket = runtime.connections.pop(session_id, None)
        if websocket is not None:
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=1000)
        runtime.end_session(session_id, now)
        return Response(status_code=204)

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

    # --- Missions (ADR-0010, SPEC 7.1) ---

    @app.get("/v1/missions")
    async def list_missions(request: Request) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        # A session token, or a login token before connecting (TASK-47)
        if (
            _bearer_session(runtime, request, runtime.mono_ns()) is None
            and _login(runtime, request) is None
        ):
            return _error(401, "unauthorized")
        assert runtime.missions is not None
        found = []
        for info in runtime.missions.all():
            version = runtime.missions.published(info.mission_id)
            if not isinstance(version, MissionVersion):
                continue  # archived, never published or invalid: not offered
            profile = version.profile
            found.append(
                {
                    "mission_id": info.mission_id,
                    "name": info.name,
                    "description": info.description,
                    "version": version.version,
                    "scenarios": [
                        {"id": s.id, "labels": list(s.labels), "severity": s.severity}
                        for s in profile.scenarios
                    ],
                    "models": sorted(
                        {m for s in profile.scenarios for m in s.required_models.values()}
                    ),
                    "frame_interval_ms": runtime.interval_for(version),
                }
            )
        return JSONResponse({"missions": found})

    @app.put("/v1/sessions/{session_id}/mission")
    async def select_mission(
        request: Request, session_id: str, body: SelectMissionRequest
    ) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        now = runtime.mono_ns()
        owner = _bearer_session(runtime, request, now)
        if owner is None:
            return _error(401, "unauthorized")
        auth = runtime.sessions.get(session_id)
        if owner != session_id or auth is None:
            return _error(404, "not_found")
        assert runtime.missions is not None
        mission = runtime.missions.published(body.mission_id)
        if not isinstance(mission, MissionVersion):
            return _error(404, "mission_not_found")
        runtime.start_mission(session_id, mission, frozenset(auth.inputs), now)
        message = runtime.mission_message(session_id)
        websocket = runtime.connections.get(session_id)
        if websocket is not None and message is not None:
            with contextlib.suppress(RuntimeError):
                await websocket.send_text(message.model_dump_json())
        ref = _mission_ref(runtime, mission)
        assert ref is not None
        return JSONResponse(ref.model_dump(mode="json"))

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

    @app.get("/v1/admin/capacity")
    async def capacity(request: Request) -> JSONResponse:
        runtime: ServerRuntime = request.app.state.runtime
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        now = runtime.mono_ns()
        sessions = []
        for session_id in sorted(runtime.session_started):
            state = runtime.frame_states.get(session_id)
            samples = list(state.samples) if state is not None else []
            sessions.append(
                {
                    "session_id": session_id,
                    "connected": session_id in runtime.connections,
                    "frame_interval_ms": (
                        state.interval_ms if state else runtime.settings.frame_interval_ms
                    ),
                    "queue_ms_avg": (
                        round(sum(q for _, q in samples) / len(samples), 1) if samples else None
                    ),
                }
            )
        return JSONResponse(
            {
                "sessions": runtime.sessions.count(),
                "max_sessions": runtime.settings.max_sessions,
                "inferences_last_minute": runtime.models.per_minute(now),
                "session_details": sessions,
            }
        )

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
    register_frame_routes(app)
    register_admin_routes(app)
    register_ui_routes(app)
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
