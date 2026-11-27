"""Admin API: missions (TASK-44A), models, deployments, settings, detect (TASK-44B).
SPEC 7.7, ADR-0009, ADR-0010.

Every route needs an admin login token (ADR-0011). Missions are edited as a
`draft` and published as a new immutable version; running sessions keep the
version they started with. Each change is appended to `admin_changes` with its
outcome only: request bodies and tokens are never recorded (SPEC 8.1).
"""

import asyncio
import contextlib
import sqlite3
import time
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from voa.config.missions import MissionDraft, MissionInfo, MissionSettings
from voa.config.model_registry import check_profile_models, load_manifests
from voa.contracts.messages import HudStyle
from voa.contracts.observation import DetectionPayload
from voa.engine.profile import ProfileError
from voa.providers.rtdetr import DEFAULT_SCORE_FLOOR, Frame
from voa.server.frames import MAX_SIDE, jpeg_size
from voa.server.hud import AdminSettings, SettingsStore
from voa.server.inference import Deployment, ModelUnavailable

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


class DeployRequest(BaseModel):
    model_config = REQUEST_CONFIG
    model_id: str = Field(min_length=1, max_length=128)
    device: Literal["cpu", "cuda"] = "cpu"
    score_floor: float = Field(default=DEFAULT_SCORE_FLOOR, ge=0.0, le=1.0)


class SettingsRequest(BaseModel):
    """Any subset; given parts replace the current ones."""

    model_config = REQUEST_CONFIG
    frame_interval_ms: int | None = Field(default=None, ge=100, le=5000)
    hud_styles: dict[str, HudStyle] | None = None
    hud_style_map: dict[str, str] | None = None


def _no_gpu(error: ModelUnavailable) -> bool:
    cause = error.__cause__
    return cause is not None and type(cause).__name__ == "VisionUnavailable"


def models_in_use(runtime: "ServerRuntime") -> dict[str, list[str]]:
    """model_id -> missions needing it: latest published versions and running sessions."""
    used: dict[str, set[str]] = {}
    profiles = [m.profile for m in runtime.session_missions.values()]
    if runtime.missions is not None:
        for info in runtime.missions.all():
            published = runtime.missions.published(info.mission_id)
            if published is not None and not isinstance(published, ProfileError):
                profiles.append(published.profile)
    for profile in profiles:
        for scenario in profile.scenarios:
            for model_id in scenario.required_models.values():
                used.setdefault(model_id, set()).add(profile.profile_id)
    return {model_id: sorted(missions) for model_id, missions in used.items()}


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

    # --- models and deployments (TASK-44B) ---

    @app.get("/v1/admin/models")
    async def list_models(request: Request) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        pool = runtime.models
        used = models_in_use(runtime)
        found = []
        for model_id, manifest in sorted(pool.manifests.items()):
            deployment = pool.deployments.get(model_id)
            found.append(
                {
                    "model_id": model_id,
                    "kind": manifest.kind,
                    "revision": manifest.revision,
                    "labels": list(manifest.labels),
                    "weights": await asyncio.to_thread(pool.weights_status, model_id),
                    "deployed": deployment is not None,
                    "device": deployment.device if deployment else None,
                    "score_floor": deployment.score_floor if deployment else None,
                    "used_by": used.get(model_id, []),
                }
            )
        return JSONResponse({"models": found})

    @app.post("/v1/admin/deployments")
    async def deploy_model(request: Request, body: DeployRequest) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        manifest = runtime.models.manifests.get(body.model_id)
        if manifest is None:
            return _error(404, "model_not_found")
        if manifest.kind != "object_detection":
            record(runtime, "model_deploy", body.model_id, "model_not_servable")
            return _error(400, "model_not_servable")  # trackers do not run server-side yet
        deployment = Deployment(device=body.device, score_floor=body.score_floor)
        try:
            await asyncio.to_thread(runtime.models.deploy, body.model_id, deployment)
        except ModelUnavailable as error:
            # a GPU that torch cannot see (or a CPU-only image) is not a weights problem
            code = (
                "device_unavailable"
                if body.device == "cuda" and _no_gpu(error)
                else "model_not_ready"
            )
            record(runtime, "model_deploy", body.model_id, code)
            return _error(400, code)
        record(runtime, "model_deploy", body.model_id, "ok")
        return JSONResponse(
            {"model_id": body.model_id, "deployed": True, **deployment.__dict__}, status_code=201
        )

    @app.delete("/v1/admin/deployments/{model_id}")
    async def undeploy_model(request: Request, model_id: str) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        if model_id not in runtime.models.deployments:
            return _error(404, "model_not_deployed")
        if models_in_use(runtime).get(model_id):
            record(runtime, "model_undeploy", model_id, "model_in_use")
            return _error(409, "model_in_use")
        async with runtime.models.lock(model_id):  # let a running inference finish
            runtime.models.undeploy(model_id)
        record(runtime, "model_undeploy", model_id, "ok")
        return JSONResponse({"model_id": model_id, "deployed": False})

    # --- settings (TASK-44B) ---

    @app.get("/v1/admin/settings")
    async def get_settings(request: Request) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        if not runtime.settings_valid:
            return _error(503, "settings_invalid")
        settings = runtime.current_settings
        return JSONResponse(
            {
                **settings.model_dump(mode="json"),
                "styles": {k: v.model_dump(mode="json") for k, v in settings.styles().items()},
            }
        )

    @app.put("/v1/admin/settings")
    async def put_settings(request: Request, body: SettingsRequest) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        current = runtime.current_settings
        try:
            updated = AdminSettings(
                frame_interval_ms=body.frame_interval_ms or current.frame_interval_ms,
                hud_styles=body.hud_styles if body.hud_styles is not None else current.hud_styles,
                hud_style_map=(
                    body.hud_style_map if body.hud_style_map is not None else current.hud_style_map
                ),
            )
        except ValueError:  # pydantic ValidationError is a ValueError
            return _error(400, "invalid_request")
        if updated.unknown_styles():
            record(runtime, "settings_update", "settings", "style_not_found")
            return _error(400, "style_not_found")
        SettingsStore(runtime.settings.active_config_dir, runtime.settings.frame_interval_ms).save(
            updated
        )
        await runtime.apply_settings(updated)
        record(runtime, "settings_update", "settings", "ok")
        return JSONResponse(updated.model_dump(mode="json"))

    # --- one-image detection check (TASK-44B) ---

    @app.post("/v1/admin/detect")
    async def detect(request: Request, model_id: str) -> JSONResponse:
        """Detections only, no engine, nothing stored or logged (SPEC 7.7)."""
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        if model_id not in runtime.models.deployments:
            return _error(503, "no_model_deployed")
        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        body = await request.body()
        size = jpeg_size(body)
        if (
            content_type != "image/jpeg"
            or len(body) > runtime.settings.frame_max_bytes
            or size is None
            or max(size) > MAX_SIDE
        ):
            return _error(400, "frame_rejected")
        frame = Frame("admin_detect", 0, body)

        def run() -> list[object]:
            return list(runtime.models.provider(model_id, 1.0).process(frame))

        async with runtime.models.lock(model_id):
            begin = time.perf_counter_ns()
            try:
                drafts = await asyncio.to_thread(run)
            except ModelUnavailable:
                return _error(503, "no_model_deployed")
            provider_ms = (time.perf_counter_ns() - begin) / 1_000_000
        detections = []
        for draft in drafts:
            payload = getattr(draft, "payload", None)
            if isinstance(payload, DetectionPayload):
                detections += [d.model_dump(mode="json") for d in payload.detections]
        return JSONResponse(
            {
                "model_id": model_id,
                "width": size[0],
                "height": size[1],
                "detections": detections,
                "provider_ms": round(provider_ms, 1),
            }
        )

    @app.get("/v1/admin/changes")
    async def list_changes(request: Request) -> JSONResponse:
        runtime = runtime_of(request)
        denied = _admin_guard(runtime, request)
        if denied is not None:
            return denied
        assert runtime.store is not None
        return JSONResponse({"changes": runtime.store.list_admin_changes()})
