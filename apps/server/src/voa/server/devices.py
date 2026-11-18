"""Device registry file (SPEC 7.1). Loading only; scrypt verification is TASK-17.

Unknown keys are rejected. Load failures return None and never expose file
contents, so readyz can report `devices_invalid` without leaking values.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from voa.contracts.types import MODEL_CONFIG, Id

InputCap = Literal["camera_0", "mic_0", "imu_0", "ptt_0"]  # SPEC 7.4
OutputCap = Literal["hud", "speaker", "haptic"]  # SPEC 7.4


class SecretScrypt(BaseModel):
    model_config = MODEL_CONFIG
    salt_hex: str = Field(pattern=r"^[0-9a-f]{32}$")  # 16 bytes
    digest_hex: str = Field(pattern=r"^[0-9a-f]{64}$")  # 32 bytes


class DeviceConfig(BaseModel):
    model_config = MODEL_CONFIG
    device_id: Id
    secret_scrypt: SecretScrypt
    allowed_inputs: list[InputCap]
    allowed_outputs: list[OutputCap]


class DeviceFile(BaseModel):
    model_config = MODEL_CONFIG
    devices: list[DeviceConfig]

    @model_validator(mode="after")
    def _unique_ids(self) -> "DeviceFile":
        ids = [device.device_id for device in self.devices]
        if len(ids) != len(set(ids)):
            raise ValueError("device_id must be unique")
        return self


def load_devices(path: Path) -> dict[str, DeviceConfig] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        parsed = DeviceFile.model_validate(data)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError):
        return None
    return {device.device_id: device for device in parsed.devices}
