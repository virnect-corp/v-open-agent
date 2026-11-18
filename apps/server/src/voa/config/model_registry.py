"""Model manifests and the profile cross-check (SPEC 4.9, 9; ADR-0004, CON-04).

This is a file I/O boundary like profile_loader: the engine never reads manifests.
Diagnostics name files, scenario IDs and field names only, never manifest or
profile values (model names, labels, reviewers).
"""

from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, StringConstraints, ValidationError, model_validator

from voa.contracts.types import MODEL_CONFIG, Id
from voa.engine.profile import PolicyProfile, ProfileError

ManifestKind = Literal["object_detection", "track_state"]
Label = Annotated[str, StringConstraints(min_length=1)]


class ModelManifest(BaseModel):
    model_config = MODEL_CONFIG

    model_id: Id
    revision: str = Field(min_length=1)
    kind: ManifestKind
    labels: list[Label] = Field(min_length=1)
    weights: Literal["builtin", "file"]
    sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    license: str = Field(min_length=1)
    license_evidence_url: str = Field(min_length=1)
    redistribution_allowed: bool
    reviewed_at: date
    reviewer: str = Field(min_length=1)

    @model_validator(mode="after")
    def _rules(self) -> "ModelManifest":
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("labels must be unique")
        if self.weights == "file" and self.sha256 is None:
            raise ValueError("file weights require sha256")
        if self.weights == "builtin" and self.sha256 is not None:
            raise ValueError("builtin models have no weights to hash")
        return self


_KNOWN_FIELDS = frozenset(ModelManifest.model_fields)


def _location(error: ValidationError) -> str:
    parts = set()
    for item in error.errors(include_input=False, include_context=False, include_url=False):
        location = ".".join(
            str(part) if isinstance(part, int) or part in _KNOWN_FIELDS else "[unknown]"
            for part in item["loc"]
        )
        parts.add(location or "root")
    return ",".join(sorted(parts))


def load_manifests(directory: str | Path) -> dict[str, ModelManifest] | ProfileError:
    """Read `<model_id>.yaml` files; the file name must equal model_id."""
    root = Path(directory)
    if not root.is_dir():
        return ProfileError("file_not_found", str(root))
    manifests: dict[str, ModelManifest] = {}
    for path in sorted(root.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return ProfileError("file_not_found", str(path))
        except yaml.YAMLError:
            return ProfileError("yaml_invalid", str(path))
        if not isinstance(data, dict):
            return ProfileError("yaml_invalid", f"{path}:root")
        try:
            manifest = ModelManifest.model_validate(data)
        except ValidationError as error:
            return ProfileError("profile_invalid", f"{path}:{_location(error)}")
        if manifest.model_id != path.stem:
            return ProfileError("profile_invalid", f"{path}:model_id does not match file name")
        manifests[manifest.model_id] = manifest
    return manifests


def check_profile_models(
    profile: PolicyProfile, manifests: dict[str, ModelManifest]
) -> ProfileError | None:
    """Every required model is registered, has the right kind and knows the labels."""
    violations: list[str] = []
    for scenario in profile.scenarios:
        for kind in scenario.required_kinds:
            field = f"required_models.{kind}"
            manifest = manifests.get(scenario.required_models[kind])
            if manifest is None:
                violations.append(f"{scenario.id}: {field}: model not registered")
            elif manifest.kind != kind:
                violations.append(f"{scenario.id}: {field}: kind mismatch")
            elif not set(scenario.labels) <= set(manifest.labels):
                violations.append(f"{scenario.id}: labels: not in {field} manifest")
    if violations:
        return ProfileError("profile_invalid", "; ".join(violations))
    return None
