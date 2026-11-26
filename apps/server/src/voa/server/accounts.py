"""Accounts: the fixed administrator and self-registered wearers (ADR-0011, SPEC 7.1).

- Passwords are stored as scrypt salt and digest (same parameters as device
  secrets). Plain passwords never reach the DB or logs.
- An unknown username costs the same scrypt work as a known one, and every
  credential failure looks the same (401). Only a correct password reveals a
  `pending` account (403 `account_pending`).
- Five failures for one username within five minutes lock that username for
  five minutes (429), counted on the server monotonic clock.
- The administrator `admin` starts with the password `admin` and must change
  it before any admin API works. That default is accepted only from loopback.
- Login tokens are random, kept in memory as SHA-256 digests, and expire with
  the session access-token TTL. A restart forgets them.
"""

import hashlib
import re
import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from voa.contracts.types import MODEL_CONFIG, UtcDatetime
from voa.server.auth import NS_PER_S, token_digest, verify_scrypt
from voa.server.devices import SecretScrypt
from voa.storage.sqlite import MetadataRow, SQLiteStore

Role = Literal["admin", "wearer"]
Status = Literal["pending", "active", "disabled"]

ADMIN_USERNAME = "admin"
ADMIN_DEFAULT_PASSWORD = "admin"
USERNAME_PATTERN = re.compile(r"^[a-z0-9_-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
FAILURE_LIMIT = 5
FAILURE_WINDOW_NS = 300 * NS_PER_S
LOCK_NS = 300 * NS_PER_S

_DUMMY = SecretScrypt(salt_hex="00" * 16, digest_hex="00" * 32)


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex(), digest.hex()


def valid_new_password(username: str, password: str) -> bool:
    return len(password) >= MIN_PASSWORD_LENGTH and password != username


# --- REST bodies ---


class LoginRequest(BaseModel):
    model_config = MODEL_CONFIG
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class SignupRequest(BaseModel):
    model_config = MODEL_CONFIG
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    display_name: str | None = Field(default=None, max_length=64)


class PasswordChangeRequest(BaseModel):
    model_config = MODEL_CONFIG
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class LoginResponse(BaseModel):
    model_config = MODEL_CONFIG
    token: str
    token_expires_at_utc: UtcDatetime
    role: Role
    password_change_required: bool


# --- Login attempt limit ---


@dataclass
class LoginLimiter:
    """Per-username failures in a sliding window; a full window locks the username."""

    _failures: dict[str, deque[int]] = field(default_factory=dict)
    _locked_until: dict[str, int] = field(default_factory=dict)

    def locked(self, username: str, now_ns: int) -> bool:
        until = self._locked_until.get(username)
        if until is None:
            return False
        if now_ns >= until:
            del self._locked_until[username]
            self._failures.pop(username, None)
            return False
        return True

    def failed(self, username: str, now_ns: int) -> None:
        window = self._failures.setdefault(username, deque())
        window.append(now_ns)
        while window and now_ns - window[0] > FAILURE_WINDOW_NS:
            window.popleft()
        if len(window) >= FAILURE_LIMIT:
            self._locked_until[username] = now_ns + LOCK_NS

    def succeeded(self, username: str) -> None:
        self._failures.pop(username, None)


# --- Login tokens ---


@dataclass(frozen=True)
class LoginToken:
    user_id: str
    role: Role
    expires_ns: int


@dataclass
class LoginTokens:
    ttl_ns: int
    _tokens: dict[bytes, LoginToken] = field(default_factory=dict)

    def issue(self, user_id: str, role: Role, now_ns: int) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        expires = now_ns + self.ttl_ns
        self._tokens[token_digest(token)] = LoginToken(user_id, role, expires)
        return token, expires

    def lookup(self, token: str, now_ns: int) -> LoginToken | None:
        found = self._tokens.get(token_digest(token))
        if found is None or now_ns >= found.expires_ns:
            return None
        return found

    def revoke_user(self, user_id: str) -> None:
        for digest in [d for d, t in self._tokens.items() if t.user_id == user_id]:
            del self._tokens[digest]


# --- Account store ---

AuthResult = Literal["ok", "unauthorized", "pending", "locked"]


@dataclass
class Accounts:
    store: SQLiteStore
    limiter: LoginLimiter = field(default_factory=LoginLimiter)

    def ensure_admin(self, now_utc: datetime) -> None:
        """Create `admin` with the default password once; never reset an existing one."""
        if self.store.get_user_by_username(ADMIN_USERNAME) is not None:
            return
        salt, digest = hash_password(ADMIN_DEFAULT_PASSWORD)
        stamp = _utc(now_utc)
        self.store.save_user(
            {
                "user_id": _new_user_id(),
                "username": ADMIN_USERNAME,
                "display_name": None,
                "role": "admin",
                "status": "active",
                "password_salt_hex": salt,
                "password_digest_hex": digest,
                "password_change_required": 1,
                "created_at_utc": stamp,
                "updated_at_utc": stamp,
            }
        )

    def admin_has_default_password(self) -> bool:
        admin = self.store.get_user_by_username(ADMIN_USERNAME)
        return admin is not None and _matches(ADMIN_DEFAULT_PASSWORD, admin)

    def signup(
        self, request: SignupRequest, *, requires_approval: bool, now_utc: datetime
    ) -> tuple[str, Status] | Literal["invalid", "taken"]:
        if not USERNAME_PATTERN.fullmatch(request.username) or request.username == ADMIN_USERNAME:
            return "invalid"
        if not valid_new_password(request.username, request.password):
            return "invalid"
        if self.store.get_user_by_username(request.username) is not None:
            return "taken"
        salt, digest = hash_password(request.password)
        status: Status = "pending" if requires_approval else "active"
        user_id, stamp = _new_user_id(), _utc(now_utc)
        self.store.save_user(
            {
                "user_id": user_id,
                "username": request.username,
                "display_name": request.display_name,
                "role": "wearer",
                "status": status,
                "password_salt_hex": salt,
                "password_digest_hex": digest,
                "password_change_required": 0,
                "created_at_utc": stamp,
                "updated_at_utc": stamp,
            }
        )
        return user_id, status

    def authenticate(
        self, username: str, password: str, now_ns: int
    ) -> tuple[AuthResult, MetadataRow | None]:
        """Same scrypt cost for every path; `pending` only after a correct password."""
        if self.limiter.locked(username, now_ns):
            return "locked", None
        user = self.store.get_user_by_username(username)
        ok = _matches(password, user)
        if user is None or not ok or user["status"] == "disabled":
            self.limiter.failed(username, now_ns)
            return "unauthorized", None
        self.limiter.succeeded(username)
        if user["status"] == "pending":
            return "pending", user
        return "ok", user

    def change_password(self, user_id: str, current: str, new: str, now_utc: datetime) -> bool:
        user = self.store.get_user(user_id)
        if user is None or not _matches(current, user):
            return False
        if not valid_new_password(str(user["username"]), new) or new == current:
            return False
        salt, digest = hash_password(new)
        user.update(
            password_salt_hex=salt,
            password_digest_hex=digest,
            password_change_required=0,
            updated_at_utc=_utc(now_utc),
        )
        self.store.save_user(user)
        return True

    def set_status(self, user_id: str, status: Status, now_utc: datetime) -> MetadataRow | None:
        user = self.store.get_user(user_id)
        if user is None or user["role"] != "wearer":
            return None
        user.update(status=status, updated_at_utc=_utc(now_utc))
        self.store.save_user(user)
        return user

    def reset_password(self, user_id: str, now_utc: datetime) -> str | None:
        """Temporary password; the wearer must change it at the next login."""
        user = self.store.get_user(user_id)
        if user is None or user["role"] != "wearer":
            return None
        temporary = secrets.token_urlsafe(9)
        salt, digest = hash_password(temporary)
        user.update(
            password_salt_hex=salt,
            password_digest_hex=digest,
            password_change_required=1,
            updated_at_utc=_utc(now_utc),
        )
        self.store.save_user(user)
        return temporary


def public_user(row: MetadataRow) -> dict[str, object]:
    """Fields an administrator may see: never the salt or digest."""
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "status": row["status"],
        "password_change_required": bool(row["password_change_required"]),
        "created_at_utc": row["created_at_utc"],
    }


def _matches(password: str, user: MetadataRow | None) -> bool:
    stored = (
        SecretScrypt(
            salt_hex=str(user["password_salt_hex"]), digest_hex=str(user["password_digest_hex"])
        )
        if user is not None
        else _DUMMY
    )
    return verify_scrypt(password, stored) and user is not None


def _new_user_id() -> str:
    return f"usr_{secrets.token_urlsafe(12)}"


def _utc(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "ADMIN_USERNAME",
    "Accounts",
    "LoginLimiter",
    "LoginRequest",
    "LoginResponse",
    "LoginTokens",
    "PasswordChangeRequest",
    "SignupRequest",
    "public_user",
]
