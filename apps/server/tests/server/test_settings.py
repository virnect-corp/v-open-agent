"""TASK-16 settings from environment (SPEC 8.3; COM-06, DAT-03, DAT-04)."""

from pathlib import Path

import pytest

from voa.server.settings import Settings, SettingsError

BASE = Path("/srv/apps/server")


def load(**env: str) -> Settings:
    return Settings.from_env({f"VOA_{k}": v for k, v in env.items()}, base_dir=BASE)


def test_defaults_match_spec() -> None:
    s = load()
    assert (s.profile, s.bind, s.port, s.allow_insecure_dev) == (
        "dev-mock",
        "127.0.0.1",
        8765,
        True,
    )
    assert (s.tls_cert, s.tls_key) == (None, None)
    assert s.policy_profile == BASE / "config/profiles/indoor_research_v0.yaml"
    assert s.models_dir == BASE / "config/models"
    assert s.model_weights_dir == BASE / "var/models"
    assert s.devices_file == BASE / "config/devices.yaml"
    assert s.db_path == BASE / "var/voa.sqlite3"
    assert (s.tick_interval_ms, s.heartbeat_ms, s.client_watchdog_ms) == (100, 1000, 3000)
    assert (s.token_ttl_s, s.refresh_token_ttl_s) == (900, 86400)
    assert (s.input_queue_max, s.metadata_retention_days) == (64, 7)
    assert (s.raw_media_storage, s.external_network, s.model_auto_download) == (
        False,
        False,
        False,
    )


def test_absolute_paths_are_kept_and_relative_paths_use_base_dir() -> None:
    s = load(DB_PATH="/data/voa.sqlite3", MODELS_DIR="other/models", MODEL_WEIGHTS_DIR="/models")
    assert s.db_path == Path("/data/voa.sqlite3")
    assert s.models_dir == BASE / "other/models"
    assert s.model_weights_dir == Path("/models")


def test_other_environment_variables_are_ignored() -> None:
    assert Settings.from_env({"HOME": "/x", "VOA_PORT": "9000"}, base_dir=BASE).port == 9000


# --- Numeric cross-validation (start is refused) ---


@pytest.mark.parametrize(
    ("env", "field"),
    [
        ({"TICK_INTERVAL_MS": "0"}, "VOA_TICK_INTERVAL_MS"),
        ({"HEARTBEAT_MS": "0"}, "VOA_HEARTBEAT_MS"),
        ({"HEARTBEAT_MS": "2000", "CLIENT_WATCHDOG_MS": "3999"}, "VOA_CLIENT_WATCHDOG_MS"),
        ({"TOKEN_TTL_S": "0"}, "VOA_TOKEN_TTL_S"),
        ({"TOKEN_TTL_S": "100", "REFRESH_TOKEN_TTL_S": "99"}, "VOA_REFRESH_TOKEN_TTL_S"),
        ({"INPUT_QUEUE_MAX": "0"}, "VOA_INPUT_QUEUE_MAX"),
        ({"METADATA_RETENTION_DAYS": "0"}, "VOA_METADATA_RETENTION_DAYS"),
        ({"PORT": "0"}, "VOA_PORT"),
        ({"PORT": "70000"}, "VOA_PORT"),
    ],
)
def test_numeric_rules(env: dict[str, str], field: str) -> None:
    with pytest.raises(SettingsError) as error:
        load(**env)
    assert field in error.value.fields


def test_boundary_values_are_accepted() -> None:
    s = load(
        HEARTBEAT_MS="1500", CLIENT_WATCHDOG_MS="3000", TOKEN_TTL_S="60", REFRESH_TOKEN_TTL_S="60"
    )
    assert (s.client_watchdog_ms, s.refresh_token_ttl_s) == (3000, 60)


@pytest.mark.parametrize(
    ("env", "field"),
    [
        ({"PORT": "abc"}, "VOA_PORT"),
        ({"ALLOW_INSECURE_DEV": "yes"}, "VOA_ALLOW_INSECURE_DEV"),
        ({"PROFILE": "prod"}, "VOA_PROFILE"),
    ],
)
def test_malformed_values_name_the_field_without_echoing_the_value(
    env: dict[str, str], field: str
) -> None:
    with pytest.raises(SettingsError) as error:
        load(**env)
    assert field in str(error.value)
    assert list(env.values())[0] not in str(error.value)


# --- Safety switches (DAT-04, AGENTS rules 5 and 8) ---


def test_raw_media_storage_refuses_start() -> None:
    with pytest.raises(SettingsError) as error:
        load(RAW_MEDIA_STORAGE="true")
    assert "VOA_RAW_MEDIA_STORAGE" in error.value.fields


@pytest.mark.parametrize("name", ["MODEL_AUTO_DOWNLOAD", "EXTERNAL_NETWORK"])
def test_unimplemented_network_switches_refuse_start(name: str) -> None:
    with pytest.raises(SettingsError) as error:
        load(**{name: "true"})
    assert f"VOA_{name}" in error.value.fields


def test_insecure_dev_only_on_loopback() -> None:
    assert load(BIND="0.0.0.0", ALLOW_INSECURE_DEV="false", EXPOSURE="loopback").bind == "0.0.0.0"
    with pytest.raises(SettingsError) as error:
        load(BIND="0.0.0.0", ALLOW_INSECURE_DEV="true")
    assert "VOA_ALLOW_INSECURE_DEV" in error.value.fields


def test_pilot_requires_existing_cert_and_key(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as error:
        load(PROFILE="pilot", ALLOW_INSECURE_DEV="false")
    assert {"VOA_TLS_CERT", "VOA_TLS_KEY"} <= set(error.value.fields)

    with pytest.raises(SettingsError):
        load(
            PROFILE="pilot",
            ALLOW_INSECURE_DEV="false",
            TLS_CERT=str(tmp_path / "missing.crt"),
            TLS_KEY=str(tmp_path / "missing.key"),
        )

    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    s = load(PROFILE="pilot", ALLOW_INSECURE_DEV="false", TLS_CERT=str(cert), TLS_KEY=str(key))
    assert (s.tls_cert, s.tls_key) == (cert, key)


def test_network_exposure_requires_tls_and_loopback_declaration_does_not(tmp_path: Path) -> None:
    """ADR-0009: TLS unless only the host loopback can reach the port (TASK-40)."""
    assert load().exposure == "loopback" and not load().tls_required
    with pytest.raises(SettingsError) as error:
        load(BIND="0.0.0.0", ALLOW_INSECURE_DEV="false")  # inferred: network
    assert {"VOA_TLS_CERT", "VOA_TLS_KEY"} <= set(error.value.fields)
    compose = load(BIND="0.0.0.0", ALLOW_INSECURE_DEV="false", EXPOSURE="loopback")
    assert compose.exposure == "loopback" and not compose.tls_required
    with pytest.raises(SettingsError) as error:
        load(EXPOSURE="network")  # loopback bind declared as exposed: still TLS
    assert "VOA_TLS_CERT" in error.value.fields
    with pytest.raises(SettingsError) as error:
        load(EXPOSURE="public")
    assert "VOA_EXPOSURE" in error.value.fields
    cert, key = tmp_path / "c", tmp_path / "k"
    cert.write_text("c", encoding="utf-8")
    key.write_text("k", encoding="utf-8")
    lan = load(BIND="0.0.0.0", ALLOW_INSECURE_DEV="false", TLS_CERT=str(cert), TLS_KEY=str(key))
    assert lan.exposure == "network" and lan.tls_required


def test_pilot_refuses_insecure_dev(tmp_path: Path) -> None:
    cert, key = tmp_path / "c", tmp_path / "k"
    cert.write_text("c", encoding="utf-8")
    key.write_text("k", encoding="utf-8")
    with pytest.raises(SettingsError) as error:
        load(PROFILE="pilot", ALLOW_INSECURE_DEV="true", TLS_CERT=str(cert), TLS_KEY=str(key))
    assert "VOA_ALLOW_INSECURE_DEV" in error.value.fields
