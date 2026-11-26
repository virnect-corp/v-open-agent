"""Mission store: the purpose units a wearer picks (ADR-0010, SPEC 7.1, 7.7).

A mission version is one engine `PolicyProfile` (`profile_id = mission_id`,
`profile_version = version`), so a decision's `policy_version` reads
`<mission_id>@<version>`. Layout under `VOA_ACTIVE_CONFIG_DIR/missions/<id>/`:

- `mission.yaml`: `{mission_id, name, description, status, latest_version}`
  with status `draft` (never published), `published` or `archived`
- `v<N>.yaml`: a published version, in the policy profile format (SPEC 4.9)
- `draft.yaml`: the version being edited (written by TASK-44)

Published versions are never rewritten: an archived mission keeps them so past
decisions stay explainable. `import_profiles` turns each `config/profiles/*.yaml`
into mission v1 once; existing missions are left untouched.
Errors never contain file contents.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from voa.config.model_registry import check_profile_models, load_manifests
from voa.config.profile_loader import load_profile
from voa.engine.profile import PolicyProfile, ProfileError

MissionStatus = Literal["draft", "published", "archived"]
MISSION_ID = re.compile(r"^[a-z0-9_-]{2,64}$")


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
        return MissionVersion(info=info, version=number, profile=profile)

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


def _write(path: Path, data: dict[str, object]) -> None:
    """Write via a temporary file so a reader never sees half a file."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(path)
