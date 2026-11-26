"""Admin transport and display settings, and HUD style selection (SPEC 7.4, 7.7; TASK-44B).

Stored at `VOA_ACTIVE_CONFIG_DIR/settings.yaml`. A missing file means the defaults;
an unreadable or invalid one also falls back to the defaults (fail-safe display)
and the admin API answers `503 settings_invalid` until an admin saves new settings.
"""

import re
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from voa.contracts.messages import Flash, HudStyle, Presentation

STYLE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
ATTENTION_DEFAULT = "attention_default"
DEGRADED_DEFAULT = "degraded_default"
NOTICE_KEY = "degraded"  # hud_style_map key for notices (SPEC 7.4)

DEFAULT_STYLES: dict[str, HudStyle] = {
    ATTENTION_DEFAULT: HudStyle(
        position="top_center",
        text_size="large",
        color="red",
        flash=Flash(mode="full_screen", count=3, on_ms=200, off_ms=200),
        show_ms=4000,
        speech=True,
        haptic="pattern_a",
    ),
    DEGRADED_DEFAULT: HudStyle(
        position="top_center",
        text_size="normal",
        color="yellow",
        flash=Flash(mode="border", count=1, on_ms=200, off_ms=200),
        show_ms=3000,
        speech=True,
        haptic="pattern_b",
    ),
}


class AdminSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    frame_interval_ms: int = Field(ge=100, le=5000)
    hud_styles: dict[str, HudStyle] = Field(default_factory=dict)  # added to / overriding defaults
    hud_style_map: dict[str, str] = Field(default_factory=dict)  # scenario_id or severity -> style

    @field_validator("hud_styles", "hud_style_map")
    @classmethod
    def _short_ids(cls, value: dict[str, object]) -> dict[str, object]:
        for key, item in value.items():
            if not STYLE_ID.fullmatch(key) or (
                isinstance(item, str) and not STYLE_ID.fullmatch(item)
            ):
                raise ValueError("style IDs and map keys are short identifiers")
        return value

    def styles(self) -> dict[str, HudStyle]:
        return {**DEFAULT_STYLES, **self.hud_styles}

    def unknown_styles(self) -> list[str]:
        known = self.styles()
        return sorted({s for s in self.hud_style_map.values() if s not in known})


class SettingsStore:
    def __init__(self, root: str | Path, default_interval_ms: int) -> None:
        self.path = Path(root) / "settings.yaml"
        self.default_interval_ms = default_interval_ms

    def defaults(self) -> AdminSettings:
        return AdminSettings(frame_interval_ms=self.default_interval_ms)

    def load(self) -> tuple[AdminSettings, bool]:
        """(settings, valid). Invalid files give the defaults and valid=False."""
        if not self.path.exists():
            return self.defaults(), True
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            return AdminSettings.model_validate(data), True
        except (OSError, UnicodeError, yaml.YAMLError, ValidationError, ValueError):
            return self.defaults(), False

    def save(self, settings: AdminSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".yaml.tmp")
        temporary.write_text(
            yaml.safe_dump(settings.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def choose_style(
    keys: tuple[str, ...],
    mission_map: Mapping[str, str],
    global_map: Mapping[str, str],
    styles: Mapping[str, HudStyle],
    default: str,
) -> str:
    """Mission map, then admin map; in each the first key (scenario_id, then severity) wins.

    A mapped style that does not exist is skipped, so a stale map never hides an alert.
    """
    for mapping in (mission_map, global_map):
        for key in keys:
            style_id = mapping.get(key)
            if style_id is not None and style_id in styles:
                return style_id
    return default


def present(style_id: str, styles: Mapping[str, HudStyle], text: str) -> Presentation:
    return Presentation(style_id=style_id, text=text, **styles[style_id].model_dump())
