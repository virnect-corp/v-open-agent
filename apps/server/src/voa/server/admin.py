"""Admin mission API (SPEC 7.7, ADR-0010; TASK-44A).

Every route needs an admin login token (ADR-0011). Missions are edited as a
`draft` and published as a new immutable version; running sessions keep the
version they started with. Each change is appended to `admin_changes` with its
outcome only: request bodies and tokens are never recorded (SPEC 8.1).
"""

import asyncio
import contextlib
import sqlite3
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from voa.config.missions import MissionDraft, MissionInfo, MissionSettings
from voa.config.model_registry import check_profile_models, load_manifests
from voa.engine.profile import ProfileError
from voa.server.inference import ModelUnavailable

if TYPE_CHECKING:
    from voa.server.app import ServerRuntime

REQUEST_CONFIG = ConfigDict(extra="forbid")


class CreateMissionRequest(BaseModel):
    model_config = REQUEST_CONFIG
    mission_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    from_mission_id: str | None = Field(default=None, max_length=64)


class UpdateMissionRequest(BaseModel):
    """Any subset; `profile`/`settings` replace the draft's (started from the latest version)."""

    model_config = REQUEST_CONFIG
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    profile: dict[str, Any] | None = None
    settings: MissionSettings | None = None


def register_admin_routes(app: FastAPI) -> None:
    from voa.server.app import _admin_guard, _error  # app imports this module first

    def runtime_of(request: Request) -> "ServerRuntime":
        runtime: ServerRuntime = request.app.state.runtime
        return runtime

    def record(
        runtime: "ServerRuntime",
        action: str,
        target: str,
        result_code: str,
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> None:
        if runtime.store is None:
            return
        with contextlib.suppress(sqlite3.Error):
            runtime.store.record_admin_change(
                changed_at_utc=runtime.clock.at(runtime.mono_ns()),
                action=action,
                target=target,
                result_code=result_code,
                from_version=from_version,
                to_version=to_version,
            )

    def summary(runtime: "ServerRuntime", info: MissionInfo) -> dict[str, object]:
        assert runtime.missions is not None
        return {
            "mission_id": info.mission_id,
            "name": info.name,
            "description": info.description,
            "status": info.status,
            "latest_version": info.latest_version,
            "has_draft": runtime.missions.draft(info.mission_id) is not None,
            "sessions": runtime.mission_users(info.mission_id),
        }

    def version_body(runtime: "ServerRuntime", mission_id: str, number: int) -> dict[str, object]:
        assert runtime.missions is not None
        settings = runtime.missions.settings(mission_id, number) or MissionSettings()
        return {
            "version": number,
            "profile": runtime.missions.version_data(mission_id, number),
            "settings": settings.model_dump(mode="json"),
        }

    @app.get("/v1/admin/missions")
    async def list_missions(request: Request) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        assert runtime.missions is not None
        return JSONResponse({"missions": [summary(runtime, m) for m in runtime.missions.all()]})

    @app.post("/v1/admin/missions")
    async def create_mission(request: Request, body: CreateMissionRequest) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        assert runtime.missions is not None
        created = runtime.missions.create(
            body.mission_id, body.name, body.description, body.from_mission_id
        )
        if isinstance(created, str):
            status, code = {
                "invalid": (400, "invalid_request"),
                "exists": (409, "mission_exists"),
                "source_not_found": (404, "mission_not_found"),
            }[created]
            record(runtime, "mission_create", body.mission_id[:64], code)
            return _error(status, code)
        record(runtime, "mission_create", created.mission_id, "ok")
        return JSONResponse(summary(runtime, created), status_code=201)

    @app.get("/v1/admin/missions/{mission_id}")
    async def get_mission(request: Request, mission_id: str) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        assert runtime.missions is not None
        info = runtime.missions.get(mission_id)
        if info is None:
            return _error(404, "mission_not_found")
        draft = runtime.missions.draft(mission_id)
        published = (
            version_body(runtime, mission_id, info.latest_version)
            if info.latest_version >= 1
            else None
        )
        return JSONResponse(
            {
                **summary(runtime, info),
                "draft": draft.model_dump(mode="json") if draft is not None else None,
                "published": published,
            }
        )

    @app.put("/v1/admin/missions/{mission_id}")
    async def update_mission(
        request: Request, mission_id: str, body: UpdateMissionRequest
    ) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        store = runtime.missions
        assert store is not None
        info = store.get(mission_id)
        if info is None:
            return _error(404, "mission_not_found")
        if body.profile is not None or body.settings is not None:
            base = store.draft(mission_id)
            if base is None and info.latest_version >= 1:
                data = store.version_data(mission_id, info.latest_version) or {}
                settings = store.settings(mission_id, info.latest_version) or MissionSettings()
                base = MissionDraft(profile=data, settings=settings)
            profile = body.profile if body.profile is not None else (base and base.profile)
            if profile is None:
                return _error(400, "invalid_request")  # a new mission needs a profile first
            settings = body.settings or (base.settings if base else MissionSettings())
            store.save_draft(mission_id, MissionDraft(profile=profile, settings=settings))
        if body.name is not None or body.description is not None:
            info = store.update_info(
                mission_id,
                body.name if body.name is not None else info.name,
                body.description if body.description is not None else info.description,
            )
            assert info is not None
        record(runtime, "mission_update", mission_id, "ok", from_version=info.latest_version)
        return JSONResponse(summary(runtime, info))

    @app.delete("/v1/admin/missions/{mission_id}")
    async def archive_mission(request: Request, mission_id: str) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        assert runtime.missions is not None
        info = runtime.missions.get(mission_id)
        if info is None:
            return _error(404, "mission_not_found")
        if runtime.mission_users(mission_id) > 0:
            record(runtime, "mission_archive", mission_id, "mission_in_use")
            return _error(409, "mission_in_use")
        archived = runtime.missions.set_status(mission_id, "archived")
        assert archived is not None
        record(runtime, "mission_archive", mission_id, "ok", from_version=info.latest_version)
        return JSONResponse(summary(runtime, archived))

    async def check_draft(
        runtime: "ServerRuntime", mission_id: str, *, load_models: bool
    ) -> tuple[MissionInfo, Any, MissionSettings] | tuple[int, str]:
        """SPEC 7.7 publish steps: schema, manifests, then (publish only) weights and loading."""
        store = runtime.missions
        assert store is not None
        info = store.get(mission_id)
        if info is None:
            return 404, "mission_not_found"
        draft = store.draft(mission_id)
        if draft is None:
            return 400, "draft_missing"
        profile = store.check(mission_id, draft)
        if isinstance(profile, ProfileError):
            return 400, "profile_invalid"
        manifests = load_manifests(runtime.settings.models_dir)
        if isinstance(manifests, ProfileError):
            return 400, "profile_model_mismatch"
        if check_profile_models(profile, manifests) is not None:
            return 400, "profile_model_mismatch"
        if load_models:
            for scenario in profile.scenarios:
                model_id = scenario.required_models.get("object_detection")
                if model_id is None:
                    continue
                try:  # verifies sha256 and keeps the model loaded (auto deployment)
                    await asyncio.to_thread(runtime.models.detector, model_id)
                except ModelUnavailable:
                    return 400, "model_not_ready"
        return info, profile, draft.settings

    @app.post("/v1/admin/missions/{mission_id}:validate")
    async def validate_mission(request: Request, mission_id: str) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        checked = await check_draft(runtime, mission_id, load_models=False)
        if isinstance(checked[0], int):
            return _error(checked[0], str(checked[1]))
        info = checked[0]
        assert isinstance(info, MissionInfo)
        return JSONResponse({"valid": True, "next_version": info.latest_version + 1})

    @app.post("/v1/admin/missions/{mission_id}:publish")
    async def publish_mission(request: Request, mission_id: str) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        assert runtime.missions is not None
        checked = await check_draft(runtime, mission_id, load_models=True)
        if isinstance(checked[0], int):
            status, code = checked
            assert isinstance(code, str)
            if status != 404:
                current = runtime.missions.get(mission_id)
                before = current.latest_version if current is not None else None
                record(runtime, "mission_publish", mission_id, code, from_version=before)
            return _error(status, code)
        info, profile, settings = checked
        assert isinstance(info, MissionInfo) and isinstance(settings, MissionSettings)
        published = runtime.missions.publish(mission_id, profile, settings)
        record(
            runtime,
            "mission_publish",
            mission_id,
            "ok",
            from_version=info.latest_version or None,
            to_version=published.version,
        )
        return JSONResponse(
            {"mission_id": mission_id, "version": published.version, "status": "published"}
        )

    @app.get("/v1/admin/missions/{mission_id}/versions")
    async def mission_versions(request: Request, mission_id: str) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        assert runtime.missions is not None
        if runtime.missions.get(mission_id) is None:
            return _error(404, "mission_not_found")
        numbers = runtime.missions.versions(mission_id)
        return JSONResponse(
            {"versions": [version_body(runtime, mission_id, n) for n in reversed(numbers)]}
        )

    @app.get("/v1/admin/changes")
    async def list_changes(request: Request) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        assert runtime.store is not None
        return JSONResponse({"changes": runtime.store.list_admin_changes()})
