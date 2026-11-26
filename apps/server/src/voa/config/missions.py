"""Mission store: the purpose units a wearer picks (ADR-0010, SPEC 7.1, 7.7).

A mission version is one engine `PolicyProfile` (`profile_id = mission_id`,
`profile_version = version`), so a decision's `policy_version` reads
`<mission_id>@<version>`. Layout under `VOA_ACTIVE_CONFIG_DIR/missions/<id>/`:

- `mission.yaml`: `{mission_id, name, description, status, latest_version}`
  with status `draft` (never published), `published` or `archived`
- `v<N>.yaml`: a published version, in the policy profile format (SPEC 4.9)
- `v<N>.settings.yaml`: that version's mission settings `{frame_interval_ms, hud_style_map}`
  (TASK-44A). Missing = server defaults (profiles imported as v1 have none)
- `draft.yaml`: `{profile, settings}` being edited by an admin; removed on publish

Published versions are never rewritten: an archived mission keeps them so past
decisions stay explainable. `import_profiles` turns each `config/profiles/*.yaml`
into mission v1 once; existing missions are left untouched.
Errors never contain file contents.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from voa.config.model_registry import check_profile_models, load_manifests
from voa.config.profile_loader import load_profile, parse_profile
from voa.engine.profile import PolicyProfile, ProfileError

MissionStatus = Literal["draft", "published", "archived"]
MISSION_ID = re.compile(r"^[a-z0-9_-]{2,64}$")
STYLE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class MissionSettings(BaseModel):
    """Per-version mission settings next to the profile (ADR-0010, TASK-44A).

    `frame_interval_ms` None = the server setting. `hud_style_map` is
    `{scenario_id or severity: style_id}` and wins over the admin settings map (SPEC 7.4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    frame_interval_ms: int | None = Field(default=None, ge=100, le=5000)
    hud_style_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("hud_style_map")
    @classmethod
    def _short_identifiers(cls, value: dict[str, str]) -> dict[str, str]:
        for key, style_id in value.items():
            if not STYLE_KEY.fullmatch(key) or not STYLE_KEY.fullmatch(style_id):
                raise ValueError("hud_style_map keys and style IDs are short identifiers")
        return value


class MissionDraft(BaseModel):
    """What an admin edits: profile data (checked on validate/publish) and settings."""

    model_config = ConfigDict(extra="forbid")
    profile: dict[str, Any]
    settings: MissionSettings = Field(default_factory=MissionSettings)


@dataclass(frozen=True)
class MissionInfo:
    mission_id: str
    name: str
    description: str
    status: MissionStatus
    latest_version: int  # 0 = never published

    @property
    def selectable(self) -> bool:
        return self.status == "published" and self.latest_version >= 1


@dataclass(frozen=True)
class MissionVersion:
    info: MissionInfo
    version: int
    profile: PolicyProfile
    settings: MissionSettings = field(default_factory=MissionSettings)


class MissionStore:
    def __init__(self, root: str | Path, models_dir: str | Path) -> None:
        self.root = Path(root) / "missions"
        self.models_dir = Path(models_dir)

    # --- read ---

    def all(self) -> list[MissionInfo]:
        if not self.root.is_dir():
            return []
        found = []
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
            info = self.get(folder.name)
            if info is not None:
                found.append(info)
        return found

    def get(self, mission_id: str) -> MissionInfo | None:
        if not MISSION_ID.fullmatch(mission_id):
            return None
        path = self.root / mission_id / "mission.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return None
        if not isinstance(data, dict) or data.get("mission_id") != mission_id:
            return None
        status = data.get("status")
        version = data.get("latest_version")
        if status not in ("draft", "published", "archived") or not isinstance(version, int):
            return None
        return MissionInfo(
            mission_id=mission_id,
            name=str(data.get("name") or mission_id),
            description=str(data.get("description") or ""),
            status=status,
            latest_version=version,
        )

    def published(
        self, mission_id: str, version: int | None = None
    ) -> MissionVersion | ProfileError | None:
        """A selectable mission's published version (latest by default), validated.

        None when the mission is unknown, archived or never published; a ProfileError
        when its file is unreadable or disagrees with the model manifests.
        """
        info = self.get(mission_id)
        if info is None or not info.selectable:
            return None
        number = info.latest_version if version is None else version
        profile = self._profile(mission_id, number)
        if isinstance(profile, ProfileError):
            return profile
        settings = self.settings(mission_id, number)
        if settings is None:
            return ProfileError("profile_invalid", f"{mission_id}:v{number}:settings")
        return MissionVersion(info=info, version=number, profile=profile, settings=settings)

    def settings(self, mission_id: str, version: int) -> MissionSettings | None:
        """A version's settings; defaults when it has none, None when the file is broken."""
        path = self.root / mission_id / f"v{version}.settings.yaml"
        if not path.exists():
            return MissionSettings()
        data = _read(path)
        try:
            return MissionSettings.model_validate(data)
        except (ValidationError, ValueError):
            return None

    def versions(self, mission_id: str) -> list[int]:
        """Published version numbers, oldest first (archived missions keep theirs)."""
        info = self.get(mission_id)
        if info is None:
            return []
        folder = self.root / mission_id
        return [n for n in range(1, info.latest_version + 1) if (folder / f"v{n}.yaml").exists()]

    def version_data(self, mission_id: str, version: int) -> dict[str, Any] | None:
        """Raw profile data of a published version, for the admin page."""
        data = _read(self.root / mission_id / f"v{version}.yaml")
        return data if isinstance(data, dict) else None

    def draft(self, mission_id: str) -> MissionDraft | None:
        if self.get(mission_id) is None:
            return None
        data = _read(self.root / mission_id / "draft.yaml")
        if not isinstance(data, dict):
            return None
        try:
            return MissionDraft.model_validate(data)
        except (ValidationError, ValueError):
            return None

    def check(self, mission_id: str, draft: MissionDraft) -> PolicyProfile | ProfileError:
        """Schema check of a draft as the next version (SPEC 7.7 publish step 1).

        `profile_id` must be the mission ID; `profile_version` is set by the server.
        Model manifests are checked separately (step 2) so the caller can tell them apart.
        """
        info = self.get(mission_id)
        if info is None:
            return ProfileError("file_not_found", f"mission:{mission_id}")
        data = {**draft.profile, "profile_version": info.latest_version + 1}
        profile = parse_profile(data, f"{mission_id}:draft")
        if isinstance(profile, ProfileError):
            return profile
        if profile.profile_id != mission_id:
            return ProfileError("profile_invalid", f"{mission_id}:draft:profile_id")
        return profile

    def _profile(self, mission_id: str, version: int) -> PolicyProfile | ProfileError:
        profile = load_profile(self.root / mission_id / f"v{version}.yaml")
        if isinstance(profile, ProfileError):
            return profile
        if profile.profile_id != mission_id or profile.profile_version != version:
            return ProfileError("profile_invalid", f"{mission_id}:v{version}:identity")
        manifests = load_manifests(self.models_dir)
        if isinstance(manifests, ProfileError):
            return manifests
        return check_profile_models(profile, manifests) or profile

    # --- write ---

    def import_profiles(
        self, profiles_dir: str | Path, *, first: str | Path | None = None
    ) -> list[str]:
        """Each profile file becomes mission v1 if no mission has its ID yet.

        `first` (the configured `VOA_POLICY_PROFILE`) is imported before the folder,
        so it wins over another file in the folder that uses the same profile ID.
        """
        imported = []
        paths = sorted(Path(profiles_dir).glob("*.yaml"))
        if first is not None:
            paths = [Path(first), *(p for p in paths if p != Path(first))]
        for path in paths:
            profile = load_profile(path)
            if isinstance(profile, ProfileError) or not MISSION_ID.fullmatch(profile.profile_id):
                continue
            mission_id = profile.profile_id
            if (self.root / mission_id).exists():
                continue
            data = profile.model_dump(mode="json")
            data["profile_version"] = 1
            folder = self.root / mission_id
            folder.mkdir(parents=True)
            _write(folder / "v1.yaml", data)
            self._write_info(
                MissionInfo(mission_id, mission_id, "", status="published", latest_version=1)
            )
            imported.append(mission_id)
        return imported

    def create(
        self, mission_id: str, name: str, description: str, from_mission_id: str | None = None
    ) -> MissionInfo | Literal["invalid", "exists", "source_not_found"]:
        """A new `draft` mission, empty or copied from another mission's latest version."""
        if not MISSION_ID.fullmatch(mission_id):
            return "invalid"
        if (self.root / mission_id).exists():
            return "exists"
        draft: MissionDraft | None = None
        if from_mission_id is not None:
            source = self.get(from_mission_id)
            if source is None:
                return "source_not_found"
            draft = self.draft(from_mission_id)
            if draft is None and source.latest_version >= 1:
                data = self.version_data(from_mission_id, source.latest_version) or {}
                settings = self.settings(from_mission_id, source.latest_version)
                draft = MissionDraft(profile=data, settings=settings or MissionSettings())
            if draft is None:
                return "source_not_found"
            profile = {**draft.profile, "profile_id": mission_id}
            profile.pop("profile_version", None)
            draft = MissionDraft(profile=profile, settings=draft.settings)
        (self.root / mission_id).mkdir(parents=True)
        info = MissionInfo(mission_id, name, description, status="draft", latest_version=0)
        self._write_info(info)
        if draft is not None:
            self.save_draft(mission_id, draft)
        return info

    def update_info(self, mission_id: str, name: str, description: str) -> MissionInfo | None:
        info = self.get(mission_id)
        if info is None:
            return None
        updated = MissionInfo(mission_id, name, description, info.status, info.latest_version)
        self._write_info(updated)
        return updated

    def save_draft(self, mission_id: str, draft: MissionDraft) -> None:
        _write(self.root / mission_id / "draft.yaml", draft.model_dump(mode="json"))

    def publish(
        self, mission_id: str, profile: PolicyProfile, settings: MissionSettings
    ) -> MissionVersion:
        """Write the next version (already checked), mark the mission published, drop the draft.

        The profile file is written before the info file, so a crash in between leaves
        the previous version as the published one.
        """
        info = self.get(mission_id)
        assert info is not None and profile.profile_version == info.latest_version + 1
        number = profile.profile_version
        folder = self.root / mission_id
        if settings != MissionSettings():
            _write(folder / f"v{number}.settings.yaml", settings.model_dump(mode="json"))
        _write(folder / f"v{number}.yaml", profile.model_dump(mode="json"))
        updated = MissionInfo(mission_id, info.name, info.description, "published", number)
        self._write_info(updated)
        (folder / "draft.yaml").unlink(missing_ok=True)
        return MissionVersion(info=updated, version=number, profile=profile, settings=settings)

    def set_status(self, mission_id: str, status: MissionStatus) -> MissionInfo | None:
        info = self.get(mission_id)
        if info is None:
            return None
        updated = MissionInfo(
            info.mission_id, info.name, info.description, status, info.latest_version
        )
        self._write_info(updated)
        return updated

    def _write_info(self, info: MissionInfo) -> None:
        _write(
            self.root / info.mission_id / "mission.yaml",
            {
                "mission_id": info.mission_id,
                "name": info.name,
                "description": info.description,
                "status": info.status,
                "latest_version": info.latest_version,
            },
        )


def _read(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None


def _write(path: Path, data: dict[str, object]) -> None:
    """Write via a temporary file so a reader never sees half a file."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(path)
