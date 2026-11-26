"""Server settings from VOA_* environment variables (SPEC 8.3, DAT-04).

Invalid settings refuse start. Errors name the variables only, never their values.
Relative paths are resolved against the server working directory (apps/server).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

ServerProfile = Literal["dev-mock", "pilot"]
T = TypeVar("T")

DEFAULTS: dict[str, str] = {
    "VOA_PROFILE": "dev-mock",
    "VOA_BIND": "127.0.0.1",
    "VOA_EXPOSURE": "",
    "VOA_PORT": "8765",
    "VOA_ALLOW_INSECURE_DEV": "true",
    "VOA_TLS_CERT": "",
    "VOA_TLS_KEY": "",
    "VOA_POLICY_PROFILE": "config/profiles/indoor_research_v0.yaml",
    "VOA_DEFAULT_MISSION": "",
    "VOA_ACTIVE_CONFIG_DIR": "var/config",
    "VOA_MODELS_DIR": "config/models",
    "VOA_MODEL_WEIGHTS_DIR": "var/models",
    "VOA_DEVICES_FILE": "config/devices.yaml",
    "VOA_DB_PATH": "var/voa.sqlite3",
    "VOA_TICK_INTERVAL_MS": "100",
    "VOA_HEARTBEAT_MS": "1000",
    "VOA_CLIENT_WATCHDOG_MS": "3000",
    "VOA_TOKEN_TTL_S": "900",
    "VOA_REFRESH_TOKEN_TTL_S": "86400",
    "VOA_SESSION_SWEEP_INTERVAL_S": "60",
    "VOA_INPUT_QUEUE_MAX": "64",
    "VOA_METADATA_RETENTION_DAYS": "7",
    "VOA_RAW_MEDIA_STORAGE": "false",
    "VOA_EXTERNAL_NETWORK": "false",
    "VOA_MODEL_AUTO_DOWNLOAD": "false",
    "VOA_SIGNUP_ENABLED": "true",
    "VOA_FRAME_MAX_BYTES": "500000",
    "VOA_MAX_SESSIONS": "3",
    "VOA_FRAME_INTERVAL_MAX_MS": "1000",
    "VOA_FRAME_INTERVAL_MS": "333",
    "VOA_FRAME_SYNC_TIMEOUT_MS": "1000",
    "VOA_SIGNUP_REQUIRES_APPROVAL": "true",
}
LOOPBACK = "127.0.0.1"
Exposure = Literal["loopback", "network"]


class SettingsError(ValueError):
    def __init__(self, fields: list[str]) -> None:
        self.fields = sorted(set(fields))
        super().__init__("invalid settings: " + ", ".join(self.fields))


@dataclass(frozen=True)
class Settings:
    profile: ServerProfile
    bind: str
    exposure: Exposure  # loopback | network (ADR-0009; empty = inferred from bind)
    port: int
    allow_insecure_dev: bool
    tls_cert: Path | None
    tls_key: Path | None
    policy_profile: Path
    default_mission: str  # "" = the VOA_POLICY_PROFILE mission, "-" = none (ADR-0010)
    active_config_dir: Path
    models_dir: Path
    model_weights_dir: Path
    devices_file: Path
    db_path: Path
    tick_interval_ms: int
    heartbeat_ms: int
    client_watchdog_ms: int
    token_ttl_s: int
    refresh_token_ttl_s: int
    session_sweep_interval_s: int
    input_queue_max: int
    metadata_retention_days: int
    raw_media_storage: bool
    external_network: bool
    model_auto_download: bool
    signup_enabled: bool = True  # ADR-0011
    frame_max_bytes: int = 500_000  # SPEC 7.6
    frame_interval_ms: int = 333
    frame_sync_timeout_ms: int = 1000
    max_sessions: int = 3  # ADR-0010
    frame_interval_max_ms: int = 1000
    signup_requires_approval: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, base_dir: Path) -> "Settings":
        raw = {name: env.get(name, default) for name, default in DEFAULTS.items()}
        bad: list[str] = []

        def parse(name: str, convert: Callable[[str], T], fallback: T) -> T:
            try:
                return convert(raw[name])
            except ValueError:
                bad.append(name)
                return fallback

        def path(name: str) -> Path:
            value = Path(raw[name])
            return value if value.is_absolute() else base_dir / value

        def optional_path(name: str) -> Path | None:
            return path(name) if raw[name] else None

        fallback: ServerProfile = "dev-mock"
        profile = parse("VOA_PROFILE", _profile, fallback)
        settings = cls(
            profile=profile,
            bind=raw["VOA_BIND"],
            exposure=parse("VOA_EXPOSURE", lambda v: _exposure(v, raw["VOA_BIND"]), "network"),
            port=parse("VOA_PORT", int, 0),
            allow_insecure_dev=parse("VOA_ALLOW_INSECURE_DEV", _bool, False),
            tls_cert=optional_path("VOA_TLS_CERT"),
            tls_key=optional_path("VOA_TLS_KEY"),
            policy_profile=path("VOA_POLICY_PROFILE"),
            default_mission=raw["VOA_DEFAULT_MISSION"].strip(),
            active_config_dir=path("VOA_ACTIVE_CONFIG_DIR"),
            models_dir=path("VOA_MODELS_DIR"),
            model_weights_dir=path("VOA_MODEL_WEIGHTS_DIR"),
            devices_file=path("VOA_DEVICES_FILE"),
            db_path=path("VOA_DB_PATH"),
            tick_interval_ms=parse("VOA_TICK_INTERVAL_MS", int, 0),
            heartbeat_ms=parse("VOA_HEARTBEAT_MS", int, 0),
            client_watchdog_ms=parse("VOA_CLIENT_WATCHDOG_MS", int, 0),
            token_ttl_s=parse("VOA_TOKEN_TTL_S", int, 0),
            refresh_token_ttl_s=parse("VOA_REFRESH_TOKEN_TTL_S", int, 0),
            session_sweep_interval_s=parse("VOA_SESSION_SWEEP_INTERVAL_S", int, 0),
            input_queue_max=parse("VOA_INPUT_QUEUE_MAX", int, 0),
            metadata_retention_days=parse("VOA_METADATA_RETENTION_DAYS", int, 0),
            raw_media_storage=parse("VOA_RAW_MEDIA_STORAGE", _bool, True),
            external_network=parse("VOA_EXTERNAL_NETWORK", _bool, True),
            model_auto_download=parse("VOA_MODEL_AUTO_DOWNLOAD", _bool, True),
            signup_enabled=parse("VOA_SIGNUP_ENABLED", _bool, True),
            frame_max_bytes=parse("VOA_FRAME_MAX_BYTES", int, 0),
            frame_interval_ms=parse("VOA_FRAME_INTERVAL_MS", int, 0),
            frame_sync_timeout_ms=parse("VOA_FRAME_SYNC_TIMEOUT_MS", int, 0),
            max_sessions=parse("VOA_MAX_SESSIONS", int, 0),
            frame_interval_max_ms=parse("VOA_FRAME_INTERVAL_MAX_MS", int, 0),
            signup_requires_approval=parse("VOA_SIGNUP_REQUIRES_APPROVAL", _bool, True),
        )
        bad += settings._violations()
        if bad:
            raise SettingsError(bad)
        return settings

    @property
    def tls_required(self) -> bool:
        """TLS for pilot and for any network exposure (ADR-0009)."""
        return self.profile == "pilot" or self.exposure == "network"

    def _violations(self) -> list[str]:
        bad: list[str] = []
        if not 1 <= self.port <= 65535:
            bad.append("VOA_PORT")
        if self.tick_interval_ms <= 0:
            bad.append("VOA_TICK_INTERVAL_MS")
        if self.heartbeat_ms <= 0:
            bad.append("VOA_HEARTBEAT_MS")
        if self.client_watchdog_ms < 2 * self.heartbeat_ms:
            bad.append("VOA_CLIENT_WATCHDOG_MS")
        if self.token_ttl_s <= 0:
            bad.append("VOA_TOKEN_TTL_S")
        if self.refresh_token_ttl_s < self.token_ttl_s:
            bad.append("VOA_REFRESH_TOKEN_TTL_S")
        if self.session_sweep_interval_s <= 0:
            bad.append("VOA_SESSION_SWEEP_INTERVAL_S")
        if self.input_queue_max < 1:
            bad.append("VOA_INPUT_QUEUE_MAX")
        if self.frame_max_bytes <= 0:
            bad.append("VOA_FRAME_MAX_BYTES")
        if not 100 <= self.frame_interval_ms <= 5000:
            bad.append("VOA_FRAME_INTERVAL_MS")
        if self.frame_sync_timeout_ms <= 0:
            bad.append("VOA_FRAME_SYNC_TIMEOUT_MS")
        if self.max_sessions < 1:
            bad.append("VOA_MAX_SESSIONS")
        if not self.frame_interval_ms <= self.frame_interval_max_ms <= 5000:
            bad.append("VOA_FRAME_INTERVAL_MAX_MS")
        if self.metadata_retention_days < 1:
            bad.append("VOA_METADATA_RETENTION_DAYS")
        # Raw media storage has no opt-in path yet (AGENTS rule 5).
        if self.raw_media_storage:
            bad.append("VOA_RAW_MEDIA_STORAGE")
        # No external calls or downloads exist; enabling them would silently do nothing.
        if self.external_network:
            bad.append("VOA_EXTERNAL_NETWORK")
        if self.model_auto_download:
            bad.append("VOA_MODEL_AUTO_DOWNLOAD")
        if self.allow_insecure_dev and (self.profile == "pilot" or self.bind != LOOPBACK):
            bad.append("VOA_ALLOW_INSECURE_DEV")
        if self.tls_required:
            if self.tls_cert is None or not self.tls_cert.is_file():
                bad.append("VOA_TLS_CERT")
            if self.tls_key is None or not self.tls_key.is_file():
                bad.append("VOA_TLS_KEY")
        return bad


def _exposure(value: str, bind: str) -> "Exposure":
    """Who can reach the port. Empty: loopback only when bound to 127.0.0.1.

    `loopback` with another bind is an operator declaration, e.g. a container bound
    to 0.0.0.0 whose port is published on the host's 127.0.0.1 only (compose).
    """
    if value == "":
        return "loopback" if bind == LOOPBACK else "network"
    if value in ("loopback", "network"):
        return "loopback" if value == "loopback" else "network"
    raise ValueError


def _bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered not in ("true", "false"):
        raise ValueError
    return lowered == "true"


def _profile(value: str) -> ServerProfile:
    if value == "dev-mock":
        return "dev-mock"
    if value == "pilot":
        return "pilot"
    raise ValueError
