"""FastAPI composition root (SPEC 7.5, 8.3).

Importing this module never reads settings, files or the environment, and never
starts a server: configuration is loaded in the lifespan of the running app.
"""

import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from voa.config.model_registry import check_profile_models, load_manifests
from voa.config.profile_loader import load_profile
from voa.engine.profile import PolicyProfile, ProfileError
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

    @classmethod
    def build(cls, settings: Settings) -> "ServerRuntime":
        try:
            store: SQLiteStore | None = SQLiteStore(settings.db_path)
        except (sqlite3.Error, OSError):
            store = None
        return cls(
            settings=settings,
            profile=load_policy(settings),
            store=store,
            devices=load_devices(settings.devices_file),
            ids=SecureIdFactory(),
            clock=AnchoredUtcClock.now(),
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


def create_app(load_settings: Callable[[], Settings] = settings_from_environment) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = ServerRuntime.build(load_settings())
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

    return app


app = create_app()
