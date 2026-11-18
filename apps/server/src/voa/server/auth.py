"""Device credentials, session tokens and refresh rotation (SPEC 7.1, ADR-0002).

Only SHA-256 digests of tokens are kept, in memory. Plain tokens, secrets and
digests never reach the DB or logs. A restart forgets every session and token.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from pydantic import BaseModel, Field

from voa.contracts.types import MODEL_CONFIG, Id, UtcDatetime
from voa.server.devices import DeviceConfig, InputCap, OutputCap, SecretScrypt
from voa.server.runtime import SecureIdFactory

INSECURE_DEV_DEVICE = "dev_fixture"
ALL_INPUTS: frozenset[str] = frozenset({"camera_0", "mic_0", "imu_0", "ptt_0"})
ALL_OUTPUTS: frozenset[str] = frozenset({"hud", "speaker", "haptic"})
NS_PER_S = 1_000_000_000

# Used for unknown devices so a missing device costs the same scrypt work.
_DUMMY = SecretScrypt(salt_hex="00" * 16, digest_hex="00" * 32)


def verify_scrypt(secret: str, stored: SecretScrypt) -> bool:
    digest = hashlib.scrypt(
        secret.encode("utf-8"), salt=bytes.fromhex(stored.salt_hex), n=2**14, r=8, p=1, dklen=32
    )
    return hmac.compare_digest(digest, bytes.fromhex(stored.digest_hex))


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


# --- REST bodies (SPEC 7.1) ---


class Capabilities(BaseModel):
    model_config = MODEL_CONFIG
    inputs: list[InputCap]
    outputs: list[OutputCap]


class CreateSessionRequest(BaseModel):
    model_config = MODEL_CONFIG
    device_id: Id
    secret: str | None = Field(default=None, max_length=1024)
    capabilities: Capabilities


class RefreshRequest(BaseModel):
    model_config = MODEL_CONFIG
    refresh_token: str = Field(max_length=256)


class TokenPair(BaseModel):
    model_config = MODEL_CONFIG
    token: str
    token_expires_at_utc: UtcDatetime
    refresh_token: str
    refresh_token_expires_at_utc: UtcDatetime


class SessionCreated(TokenPair):
    session_id: Id
    heartbeat_ms: int
    watchdog_ms: int


# --- Credentials ---


def authenticate_device(
    request: CreateSessionRequest,
    devices: dict[str, DeviceConfig] | None,
    *,
    allow_insecure_dev: bool,
) -> tuple[frozenset[str], frozenset[str]] | None:
    """Return (allowed_inputs, allowed_outputs) or None. Failures are indistinguishable."""
    if allow_insecure_dev and request.device_id == INSECURE_DEV_DEVICE:
        return ALL_INPUTS, ALL_OUTPUTS
    device = (devices or {}).get(request.device_id)
    ok = verify_scrypt(request.secret or "", device.secret_scrypt if device else _DUMMY)
    if device is None or request.secret is None or not ok:
        return None
    return frozenset(device.allowed_inputs), frozenset(device.allowed_outputs)


# --- Sessions ---


@dataclass
class SessionAuth:
    session_id: str
    device_id: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    access_digest: bytes
    access_expires_mono_ns: int
    refresh_digest: bytes
    refresh_expires_mono_ns: int


@dataclass(frozen=True)
class IssuedTokens:
    token: str
    token_expires_mono_ns: int
    refresh_token: str
    refresh_expires_mono_ns: int


class SessionRegistry:
    """In-memory sessions for one server process (no restore after restart)."""

    def __init__(self, ids: SecureIdFactory, *, token_ttl_s: int, refresh_ttl_s: int) -> None:
        self._ids = ids
        self._token_ttl_ns = token_ttl_s * NS_PER_S
        self._refresh_ttl_ns = refresh_ttl_s * NS_PER_S
        self._sessions: dict[str, SessionAuth] = {}

    def get(self, session_id: str) -> SessionAuth | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def refresh_expired(self, now_mono_ns: int) -> list[str]:
        """Sessions that can no longer be refreshed (ADR-0005)."""
        return [
            session.session_id
            for session in self._sessions.values()
            if now_mono_ns >= session.refresh_expires_mono_ns
        ]

    def create(
        self, device_id: str, capabilities: Capabilities, now_mono_ns: int
    ) -> tuple[SessionAuth, IssuedTokens]:
        tokens = self._issue(now_mono_ns)
        session = SessionAuth(
            session_id=self._ids.new("sess"),
            device_id=device_id,
            inputs=tuple(capabilities.inputs),
            outputs=tuple(capabilities.outputs),
            access_digest=token_digest(tokens.token),
            access_expires_mono_ns=tokens.token_expires_mono_ns,
            refresh_digest=token_digest(tokens.refresh_token),
            refresh_expires_mono_ns=tokens.refresh_expires_mono_ns,
        )
        self._sessions[session.session_id] = session
        return session, tokens

    def refresh(self, session_id: str, refresh_token: str, now_mono_ns: int) -> IssuedTokens | None:
        """One-time rotation: on success both previous tokens stop working at once."""
        session = self._sessions.get(session_id)
        if session is None or now_mono_ns >= session.refresh_expires_mono_ns:
            return None
        if not hmac.compare_digest(session.refresh_digest, token_digest(refresh_token)):
            return None
        tokens = self._issue(now_mono_ns)
        session.access_digest = token_digest(tokens.token)
        session.access_expires_mono_ns = tokens.token_expires_mono_ns
        session.refresh_digest = token_digest(tokens.refresh_token)
        session.refresh_expires_mono_ns = tokens.refresh_expires_mono_ns
        return tokens

    def verify_access(self, session_id: str, token: str, now_mono_ns: int) -> bool:
        """Checked at WebSocket handshake; an expired token is invalid from its expiry on."""
        session = self._sessions.get(session_id)
        if session is None or now_mono_ns >= session.access_expires_mono_ns:
            return False
        return hmac.compare_digest(session.access_digest, token_digest(token))

    def _issue(self, now_mono_ns: int) -> IssuedTokens:
        return IssuedTokens(
            token=secrets.token_urlsafe(32),
            token_expires_mono_ns=now_mono_ns + self._token_ttl_ns,
            refresh_token=secrets.token_urlsafe(32),
            refresh_expires_mono_ns=now_mono_ns + self._refresh_ttl_ns,
        )
