"""TASK-17 device credentials, sessions and refresh rotation (SPEC 7.1, ADR-0002, ADR-0005)."""

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voa.server.app import create_app
from voa.server.auth import token_digest, verify_scrypt
from voa.server.devices import SecretScrypt, load_devices
from voa.server.settings import Settings
from voa.storage.sqlite import SQLiteStore

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
SECRET = "example-secret-change-me"  # matches config/devices.example.yaml
S = 1_000_000_000


class FakeMono:
    def __init__(self) -> None:
        self.now = 1_000 * S

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    shutil.copytree(CONFIG / "profiles", tmp_path / "config" / "profiles")
    shutil.copytree(CONFIG / "models", tmp_path / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", tmp_path / "config" / "devices.yaml")
    return tmp_path


def make_client(workdir: Path, mono: FakeMono | None = None, **env: str) -> TestClient:
    settings = Settings.from_env(
        {"VOA_TOKEN_TTL_S": "60", "VOA_REFRESH_TOKEN_TTL_S": "600"}
        | {f"VOA_{k}": v for k, v in env.items()},
        base_dir=workdir,
    )
    return TestClient(create_app(lambda: settings, mono_ns=mono or FakeMono()))


def body(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "device_id": "dev_example",
        "secret": SECRET,
        "capabilities": {"inputs": ["camera_0"], "outputs": ["hud", "speaker"]},
    }
    data.update(over)
    return data


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# --- scrypt ---


def test_scrypt_matches_example_device() -> None:
    devices = load_devices(CONFIG / "devices.example.yaml")
    assert devices is not None
    scrypt = devices["dev_example"].secret_scrypt
    assert verify_scrypt(SECRET, scrypt)
    assert not verify_scrypt(SECRET + "x", scrypt)
    assert not verify_scrypt("", scrypt)


def test_scrypt_uses_fixed_parameters() -> None:
    import hashlib

    salt = "0f" * 16
    digest = hashlib.scrypt(b"s3cret", salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
    assert verify_scrypt("s3cret", SecretScrypt(salt_hex=salt, digest_hex=digest.hex()))


# --- Session issuance (success) ---


def test_create_session_issues_tokens_and_records_session(workdir: Path) -> None:
    mono = FakeMono()
    with make_client(workdir, mono) as c:
        response = c.post("/v1/sessions", json=body())
        assert response.status_code == 200
        data = response.json()
        runtime = c.app.state.runtime  # type: ignore[attr-defined]
    assert set(data) == {
        "session_id",
        "token",
        "token_expires_at_utc",
        "refresh_token",
        "refresh_token_expires_at_utc",
        "heartbeat_ms",
        "watchdog_ms",
        "mission",  # ADR-0010: the default mission (indoor_research_v0 v1)
        "frame_interval_ms",  # SPEC 7.6
    }
    assert data["mission"] == {
        "mission_id": "indoor_research_v0",
        "version": 1,
        "name": "indoor_research_v0",
    }
    assert data["session_id"].startswith("sess_")
    assert data["token"] != data["refresh_token"]
    assert len(data["token"]) == 43 and len(data["refresh_token"]) == 43  # token_urlsafe(32)
    assert (data["heartbeat_ms"], data["watchdog_ms"]) == (1000, 3000)
    assert data["token_expires_at_utc"].endswith("Z")
    assert utc(data["token_expires_at_utc"]) == runtime.clock.at(mono.now + 60 * S)
    assert utc(data["refresh_token_expires_at_utc"]) == runtime.clock.at(mono.now + 600 * S)
    # The server's connection belongs to its event-loop thread; reopen the file here.
    with SQLiteStore(workdir / "var" / "voa.sqlite3") as store:
        row = store.get_session(data["session_id"])
    assert row is not None and row["device_id"] == "dev_example"


def test_only_token_digests_are_kept_in_memory(workdir: Path) -> None:
    with make_client(workdir) as c:
        data = c.post("/v1/sessions", json=body()).json()
        session = c.app.state.runtime.sessions.get(data["session_id"])  # type: ignore[attr-defined]
    stored = repr(vars(session))
    assert data["token"] not in stored and data["refresh_token"] not in stored
    assert session.access_digest == token_digest(data["token"])


# --- Authentication failures look identical (401) ---


@pytest.mark.parametrize(
    "request_body",
    [
        body(secret="wrong"),
        body(secret=""),
        body(device_id="dev_unknown"),
        {k: v for k, v in body().items() if k != "secret"},
    ],
)
def test_bad_credentials_are_unauthorized_without_detail(
    workdir: Path, request_body: dict[str, Any]
) -> None:
    with make_client(workdir) as c:
        response = c.post("/v1/sessions", json=request_body)
    assert (response.status_code, response.json()) == (401, {"code": "unauthorized"})


# --- Capabilities (DEV-01) ---


@pytest.mark.parametrize(
    "capabilities",
    [
        {"inputs": ["mic_0"], "outputs": ["hud"]},
        {"inputs": ["camera_0"], "outputs": ["haptic"]},
    ],
)
def test_capability_outside_registration_is_400(
    workdir: Path, capabilities: dict[str, list[str]]
) -> None:
    with make_client(workdir) as c:
        response = c.post("/v1/sessions", json=body(capabilities=capabilities))
    assert (response.status_code, response.json()) == (400, {"code": "capability_not_allowed"})


def test_empty_outputs_is_400(workdir: Path) -> None:
    with make_client(workdir) as c:
        response = c.post(
            "/v1/sessions", json=body(capabilities={"inputs": ["camera_0"], "outputs": []})
        )
    assert (response.status_code, response.json()) == (400, {"code": "no_output_capability"})


def test_capabilities_are_checked_only_after_authentication(workdir: Path) -> None:
    with make_client(workdir) as c:
        response = c.post(
            "/v1/sessions",
            json=body(secret="wrong", capabilities={"inputs": [], "outputs": []}),
        )
    assert response.status_code == 401


def test_empty_inputs_are_allowed(workdir: Path) -> None:
    with make_client(workdir) as c:
        response = c.post(
            "/v1/sessions", json=body(capabilities={"inputs": [], "outputs": ["hud"]})
        )
    assert response.status_code == 200


# --- Malformed requests never echo the input ---


@pytest.mark.parametrize(
    "request_body",
    [
        body(capabilities={"inputs": ["camera_9"], "outputs": ["hud"]}),
        body(extra="x"),
        {"device_id": "dev_example", "secret": SECRET},
        body(device_id="bad id!"),
    ],
)
def test_malformed_request_is_400_without_echo(workdir: Path, request_body: dict[str, Any]) -> None:
    with make_client(workdir) as c:
        response = c.post("/v1/sessions", json=request_body)
    assert (response.status_code, response.json()) == (400, {"code": "invalid_request"})
    assert SECRET not in response.text


def test_non_json_body_is_400(workdir: Path) -> None:
    with make_client(workdir) as c:
        response = c.post("/v1/sessions", content=b"secret=" + SECRET.encode())
    assert (response.status_code, response.json()) == (400, {"code": "invalid_request"})


# --- Refresh rotation (ADR-0002) ---


def test_refresh_rotates_both_tokens_once(workdir: Path) -> None:
    mono = FakeMono()
    with make_client(workdir, mono) as c:
        first = c.post("/v1/sessions", json=body()).json()
        sid = first["session_id"]
        mono.now += 10 * S
        response = c.post(
            f"/v1/sessions/{sid}/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert response.status_code == 200
        second = response.json()
        assert set(second) == {
            "token",
            "token_expires_at_utc",
            "refresh_token",
            "refresh_token_expires_at_utc",
        }
        assert second["token"] != first["token"]
        assert second["refresh_token"] != first["refresh_token"]
        sessions = c.app.state.runtime.sessions  # type: ignore[attr-defined]
        assert not sessions.verify_access(sid, first["token"], mono.now)  # old access revoked
        assert sessions.verify_access(sid, second["token"], mono.now)
        reuse = c.post(
            f"/v1/sessions/{sid}/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert (reuse.status_code, reuse.json()) == (401, {"code": "unauthorized"})
        again = c.post(
            f"/v1/sessions/{sid}/refresh", json={"refresh_token": second["refresh_token"]}
        )
        assert again.status_code == 200


@pytest.mark.parametrize("case", ["wrong_token", "unknown_session", "other_session"])
def test_refresh_failures_are_401(workdir: Path, case: str) -> None:
    with make_client(workdir) as c:
        a = c.post("/v1/sessions", json=body()).json()
        b = c.post("/v1/sessions", json=body()).json()
        sid, token = {
            "wrong_token": (a["session_id"], "x" * 43),
            "unknown_session": ("sess_missing", a["refresh_token"]),
            "other_session": (b["session_id"], a["refresh_token"]),
        }[case]
        response = c.post(f"/v1/sessions/{sid}/refresh", json={"refresh_token": token})
    assert (response.status_code, response.json()) == (401, {"code": "unauthorized"})


# --- Expiry (delay) ---


def test_access_token_expires_at_ttl(workdir: Path) -> None:
    mono = FakeMono()
    with make_client(workdir, mono) as c:
        data = c.post("/v1/sessions", json=body()).json()
        sessions = c.app.state.runtime.sessions  # type: ignore[attr-defined]
        sid = data["session_id"]
        assert sessions.verify_access(sid, data["token"], mono.now + 60 * S - 1)
        assert not sessions.verify_access(sid, data["token"], mono.now + 60 * S)
        assert not sessions.verify_access("sess_other", data["token"], mono.now)


def test_refresh_token_expires_at_ttl(workdir: Path) -> None:
    mono = FakeMono()
    with make_client(workdir, mono) as c:
        data = c.post("/v1/sessions", json=body()).json()
        mono.now += 600 * S
        response = c.post(
            f"/v1/sessions/{data['session_id']}/refresh",
            json={"refresh_token": data["refresh_token"]},
        )
    assert response.status_code == 401


def test_refresh_after_server_restart_is_401(workdir: Path) -> None:
    with make_client(workdir) as c:
        data = c.post("/v1/sessions", json=body()).json()
    with make_client(workdir) as restarted:
        response = restarted.post(
            f"/v1/sessions/{data['session_id']}/refresh",
            json={"refresh_token": data["refresh_token"]},
        )
    assert response.status_code == 401


# --- Insecure dev and readiness ---


def test_insecure_dev_fixture_needs_no_secret_on_loopback(workdir: Path) -> None:
    request = {
        "device_id": "dev_fixture",
        "capabilities": {"inputs": ["camera_0", "mic_0"], "outputs": ["hud", "haptic"]},
    }
    with make_client(workdir, ALLOW_INSECURE_DEV="true", BIND="127.0.0.1") as c:
        assert c.post("/v1/sessions", json=request).status_code == 200
    with make_client(workdir, ALLOW_INSECURE_DEV="false") as c:
        assert c.post("/v1/sessions", json=request).status_code == 401


def test_sessions_are_refused_while_not_ready(workdir: Path) -> None:
    (workdir / "config" / "models" / "mock-tracker.yaml").unlink()
    with make_client(workdir) as c:
        response = c.post("/v1/sessions", json=body())
    assert (response.status_code, response.json()) == (503, {"code": "not_ready"})
