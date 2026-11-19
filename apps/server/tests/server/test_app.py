"""TASK-16 app skeleton: healthz, readyz, entry point (SPEC 7.5, 8.3; COM-06, DAT-04)."""

import importlib
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voa.server.__main__ import main, uvicorn_options
from voa.server.app import create_app
from voa.server.devices import load_devices
from voa.server.logconfig import server_log_config
from voa.server.settings import Settings

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A server working directory with default profile, manifests and example devices."""
    shutil.copytree(CONFIG / "profiles", tmp_path / "config" / "profiles")
    shutil.copytree(CONFIG / "models", tmp_path / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", tmp_path / "config" / "devices.yaml")
    return tmp_path


def settings(workdir: Path, **env: str) -> Settings:
    return Settings.from_env({f"VOA_{k}": v for k, v in env.items()}, base_dir=workdir)


def client(s: Settings) -> TestClient:
    return TestClient(create_app(lambda: s))


# --- healthz / readyz success ---


def test_healthz(workdir: Path) -> None:
    with client(settings(workdir)) as c:
        response = c.get("/v1/healthz")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_readyz_ready_with_valid_configuration(workdir: Path) -> None:
    with client(settings(workdir)) as c:
        response = c.get("/v1/readyz")
    assert (response.status_code, response.json()) == (200, {"status": "ready"})
    assert (workdir / "var" / "voa.sqlite3").exists()


# --- readyz failures, each reported by item name only ---


def not_ready(s: Settings) -> str:
    with client(s) as c:
        response = c.get("/v1/readyz")
    assert response.status_code == 503
    body = response.json()
    assert set(body) == {"status", "error_code"} and body["status"] == "not_ready"
    assert str(s.policy_profile.parent) not in response.text
    return str(body["error_code"])


def test_invalid_profile_is_not_ready(workdir: Path) -> None:
    (workdir / "config" / "profiles" / "indoor_research_v0.yaml").write_text(
        "profile_id: [", encoding="utf-8"
    )
    assert not_ready(settings(workdir)) == "profile_invalid"


def test_missing_profile_is_not_ready(workdir: Path) -> None:
    assert not_ready(settings(workdir, POLICY_PROFILE="config/profiles/nope.yaml")) == (
        "profile_invalid"
    )


def test_manifest_mismatch_is_profile_invalid(workdir: Path) -> None:
    (workdir / "config" / "models" / "mock-tracker.yaml").unlink()
    assert not_ready(settings(workdir)) == "profile_invalid"


def test_missing_models_dir_is_profile_invalid(workdir: Path) -> None:
    assert not_ready(settings(workdir, MODELS_DIR="config/no-models")) == "profile_invalid"


def test_unavailable_db_is_not_ready(workdir: Path) -> None:
    (workdir / "blocked").write_text("a file, not a directory", encoding="utf-8")
    assert not_ready(settings(workdir, DB_PATH="blocked/voa.sqlite3")) == "db_unavailable"


@pytest.mark.parametrize(
    "content",
    [None, "devices: [", "devices:\n  - device_id: d1\n    unknown: 1\n"],
)
def test_missing_or_invalid_devices_is_not_ready(workdir: Path, content: str | None) -> None:
    path = workdir / "config" / "devices.yaml"
    if content is None:
        path.unlink()
    else:
        path.write_text(content, encoding="utf-8")
    assert not_ready(settings(workdir)) == "devices_invalid"


def test_first_failing_check_wins_in_spec_order(workdir: Path) -> None:
    (workdir / "config" / "devices.yaml").unlink()
    (workdir / "blocked").write_text("x", encoding="utf-8")
    assert not_ready(settings(workdir, DB_PATH="blocked/db.sqlite3")) == "db_unavailable"
    assert (
        not_ready(
            settings(
                workdir, DB_PATH="blocked/db.sqlite3", POLICY_PROFILE="config/profiles/nope.yaml"
            )
        )
        == "profile_invalid"
    )


# --- devices loader ---


def test_example_devices_file_loads() -> None:
    registry = load_devices(CONFIG / "devices.example.yaml")
    assert registry is not None
    device = registry["dev_example"]
    assert device.allowed_inputs == ["camera_0"]
    assert device.allowed_outputs == ["hud", "speaker"]


@pytest.mark.parametrize(
    "text",
    [
        "devices: []\nextra: 1\n",
        "- not a mapping\n",
        "devices:\n  - device_id: a\n    secret_scrypt: {salt_hex: zz, digest_hex: '00'}\n"
        "    allowed_inputs: [camera_0]\n    allowed_outputs: [hud]\n",
        "devices:\n  - device_id: a\n    secret_scrypt: {salt_hex: '"
        + "0" * 32
        + "', digest_hex: '"
        + "0" * 64
        + "'}\n    allowed_inputs: [camera_9]\n    allowed_outputs: [hud]\n",
        "devices:\n  - device_id: a\n    secret_scrypt: {salt_hex: '"
        + "0" * 32
        + "', digest_hex: '"
        + "0" * 64
        + "'}\n    allowed_inputs: [camera_0]\n    allowed_outputs: [projector]\n",
    ],
)
def test_invalid_devices_are_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "devices.yaml"
    path.write_text(text, encoding="utf-8")
    assert load_devices(path) is None


def test_duplicate_device_ids_are_rejected(tmp_path: Path) -> None:
    entry = (
        "  - device_id: a\n    secret_scrypt: {salt_hex: '"
        + "0" * 32
        + "', digest_hex: '"
        + "0" * 64
        + "'}\n    allowed_inputs: [camera_0]\n    allowed_outputs: [hud]\n"
    )
    path = tmp_path / "devices.yaml"
    path.write_text("devices:\n" + entry + entry, encoding="utf-8")
    assert load_devices(path) is None


# --- Entry point ---


def test_importing_the_app_module_does_not_start_or_read_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOA_POLICY_PROFILE", "/nonexistent/profile.yaml")
    monkeypatch.setenv("VOA_PORT", "not-a-number")
    module = importlib.reload(importlib.import_module("voa.server.app"))
    assert module.app is not None


def test_uvicorn_options_dev_and_pilot(tmp_path: Path) -> None:
    dev = uvicorn_options(settings(tmp_path, BIND="127.0.0.1", PORT="9000"))
    logging_options = {"log_config": server_log_config(), "access_log": False}
    assert dev == {"host": "127.0.0.1", "port": 9000, **logging_options}
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("c", encoding="utf-8")
    key.write_text("k", encoding="utf-8")
    pilot = uvicorn_options(
        settings(
            tmp_path,
            PROFILE="pilot",
            ALLOW_INSECURE_DEV="false",
            BIND="0.0.0.0",
            TLS_CERT=str(cert),
            TLS_KEY=str(key),
        )
    )
    assert pilot == {
        **logging_options,
        "host": "0.0.0.0",
        "port": 8765,
        "ssl_certfile": str(cert),
        "ssl_keyfile": str(key),
    }


def test_main_refuses_invalid_settings_without_starting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VOA_RAW_MEDIA_STORAGE", "true")
    started: list[object] = []
    monkeypatch.setattr("voa.server.__main__.uvicorn.run", lambda *a, **k: started.append(a))
    assert main() == 2
    assert started == []
    assert "VOA_RAW_MEDIA_STORAGE" in capsys.readouterr().err


def test_main_starts_uvicorn_with_the_app_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VOA_RAW_MEDIA_STORAGE", raising=False)
    monkeypatch.setenv("VOA_PORT", "9123")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr("voa.server.__main__.uvicorn.run", lambda *a, **k: calls.append((a, k)))
    assert main() == 0
    assert calls == [
        (
            ("voa.server.app:app",),
            {
                "host": "127.0.0.1",
                "port": 9123,
                "log_config": server_log_config(),
                "access_log": False,
            },
        )
    ]


# --- TASK-28 log hardening (SPEC 8.2, DAT-02) ---


def test_main_uses_the_json_allowlist_log_config(tmp_path: Path) -> None:
    import logging.config

    from voa.server.logconfig import SERVER_LOGGERS, server_log_config
    from voa.storage.logging import JsonLogFormatter

    options = uvicorn_options(settings(tmp_path))
    assert options["access_log"] is False
    config = options["log_config"]
    assert config == server_log_config()
    assert config["disable_existing_loggers"] is False
    assert set(config["loggers"]) == set(SERVER_LOGGERS)
    assert all(entry["level"] == "INFO" for entry in config["loggers"].values())
    logging.config.dictConfig(config)
    try:
        handler = logging.getLogger("uvicorn.error").handlers[0]
        assert isinstance(handler.formatter, JsonLogFormatter)
    finally:
        for name in SERVER_LOGGERS:
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.propagate = True
            logger.setLevel(logging.NOTSET)


def test_unstructured_records_get_the_logger_name_as_event() -> None:
    import logging

    from voa.server.logconfig import LoggerEventFilter
    from voa.storage.logging import JsonLogFormatter

    record = logging.LogRecord("uvicorn.error", logging.ERROR, "f", 1, "boom %s", ("tok",), None)
    assert LoggerEventFilter().filter(record)
    assert JsonLogFormatter().format(record) == '{"event":"uvicorn.error"}'
    structured = logging.LogRecord("voa.x", logging.INFO, "f", 1, "", None, None)
    structured.__dict__["event"] = "late_ack"
    LoggerEventFilter().filter(structured)
    assert structured.__dict__["event"] == "late_ack"


@pytest.mark.parametrize("before", [0, 5, 10, 20, 30])  # NOTSET, TRACE, DEBUG, INFO, WARNING
def test_pin_log_levels_raises_to_info_but_never_lowers(before: int) -> None:
    import logging

    from voa.server.logconfig import SERVER_LOGGERS, pin_log_levels

    saved = {name: logging.getLogger(name).level for name in SERVER_LOGGERS}
    try:
        for name in SERVER_LOGGERS:
            logging.getLogger(name).setLevel(before)
        pin_log_levels()
        for name in SERVER_LOGGERS:
            assert logging.getLogger(name).level == max(before, logging.INFO)
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
