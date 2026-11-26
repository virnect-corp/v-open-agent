"""TASK-23 privacy leak check after an end-to-end run (DAT-01, DAT-02, G4).

A real server and simulator run through the paths that carry sensitive values:
session secret, access/refresh tokens (including rotated ones), alert text,
OCR/ASR text, sensor detail codes, malformed messages and raw binary frames.
Every value is a known canary. Afterwards every file under tmp_path (SQLite DB,
WAL/SHM), captured stdout/stderr, every log record at INFO and above (message,
args, extras, exception text) and the simulator output log must contain none
of them, in raw or JSON-escaped form.
"""

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.integration.live_server import SECRET, LiveServer, make_workdir
from voa.contracts.observation import (
    AsrPayload,
    Detection,
    DetectionPayload,
    HealthPayload,
    ModelRef,
    ObservationDraft,
    OcrPayload,
)
from voa.server.logconfig import server_log_config
from voa.simulator.adapter import ClientConfig, GlassClient, post_json

ALERT_TEXT = "전방 통로에 장애물이 보입니다."
WRONG_SECRET = "canary-wrong-secret-5c1e"
FRAME_CANARY = "canary-frame-91ab"
OCR_CANARY = "canary-ocr-비상구-잠김-77d2"
ASR_CANARY = "canary-asr-경고-꺼-3f0c"
DETAIL_CANARY = "canary-detail-a4e9"
SCHEMA_CANARY = "canary-schema-0b6d"
RAW_CANARY = b"canary-raw-media-\xff\xd8\xff-e1f7"
FAST_BACKOFF = (0.05, 0.1, 0.2, 0.4)
WARNING_TTL_MS = 400  # short TTL so an unacked alert becomes unknown_delivery


# --- scanner ---


def forms(value: str) -> list[bytes]:
    """Raw UTF-8 and the JSON-escaped (ensure_ascii) form of a value."""
    escaped = json.dumps(value)[1:-1]
    return sorted({value.encode("utf-8"), escaped.encode("ascii")})


@dataclass
class Leaks:
    needles: dict[str, list[bytes]] = field(default_factory=dict)
    found: list[tuple[str, str]] = field(default_factory=list)

    def watch(self, label: str, value: str | bytes) -> None:
        if isinstance(value, bytes):
            self.needles[label] = [value]
        else:
            self.needles[label] = forms(value)

    def scan(self, where: str, data: bytes) -> None:
        for label, needles in self.needles.items():
            if any(needle in data for needle in needles):
                self.found.append((where, label))

    def scan_tree(self, root: Path, skip: Iterable[Path] = ()) -> list[Path]:
        """Scan every file under root except the given input folders (copied config)."""
        skipped = tuple(skip)
        files = sorted(
            p
            for p in root.rglob("*")
            if p.is_file() and not any(p.is_relative_to(folder) for folder in skipped)
        )
        for path in files:
            self.scan(str(path.relative_to(root)), path.read_bytes())
        return files


def record_dump(record: logging.LogRecord) -> str:
    """Everything a handler could emit: message, args, every extra and exception text."""
    parts = [repr(value) for value in record.__dict__.values()]
    if record.exc_info:
        parts.append(logging.Formatter().formatException(record.exc_info))
    with_message = record.getMessage() if record.args or record.msg else ""
    return "\n".join([with_message, *parts])


class RecordCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.dumps: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.dumps.append(record_dump(record))

    def attach(self) -> None:
        for name in LOGGERS:
            logger = logging.getLogger(name)
            if self not in logger.handlers:
                logger.addHandler(self)


LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "websockets", "voa", "fastapi")


@pytest.fixture
def records() -> Iterator[RecordCollector]:
    """Collect at INFO from every logger, also ones that do not propagate to root.

    The server log config replaces handlers of the server loggers, so tests call
    `attach()` again after start; the original logging state is restored afterwards.
    """
    collector = RecordCollector()
    loggers = [logging.getLogger(name) for name in LOGGERS]
    saved = [(lg, list(lg.handlers), lg.propagate, lg.level, lg.disabled) for lg in loggers]
    logging.getLogger().setLevel(logging.INFO)
    collector.attach()
    yield collector
    for logger, handlers, propagate, level, disabled in saved:
        logger.handlers[:] = handlers
        logger.propagate = propagate
        logger.setLevel(level)
        logger.disabled = disabled


# --- scanner self-test (failure path) ---


def test_scanner_finds_planted_values_in_files_logs_and_escaped_form(tmp_path: Path) -> None:
    leaks = Leaks()
    leaks.watch("ocr", OCR_CANARY)
    leaks.watch("raw", RAW_CANARY)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.log").write_text(json.dumps({"x": OCR_CANARY}), encoding="utf-8")
    (tmp_path / "b.bin").write_bytes(b"\x00" + RAW_CANARY)
    leaks.scan_tree(tmp_path)
    record = logging.LogRecord("t", logging.INFO, "f", 1, "", None, None)
    record.__dict__["payload"] = {"text": OCR_CANARY}
    leaks.scan("record", record_dump(record).encode("utf-8"))
    assert sorted(leaks.found) == [("b.bin", "raw"), ("record", "ocr"), ("sub/a.log", "ocr")]
    assert len(forms(OCR_CANARY)) == 2  # non-ASCII text has a distinct escaped form


# --- end-to-end run ---


class SilentClient(GlassClient):
    """Never ACKs, so its alert expires as unknown_delivery and a late ACK is logged."""

    async def _ack(self, ws: Any, alert_id: str, status: Any) -> None:
        return None


def obstacle(seq: int) -> ObservationDraft:
    return ObservationDraft(
        stream_id="camera_0",
        seq=seq,
        capture_ts_monotonic_ns=time.monotonic_ns(),
        model=ModelRef(id="mock-detector", revision="0"),
        payload=DetectionPayload(
            kind="object_detection",
            frame_id=f"{FRAME_CANARY}-{seq}",
            detections=[Detection(label="obstacle", bbox=(0.3, 0.5, 0.6, 0.9), score=0.9)],
        ),
    )


def short_ttl_profile(workdir: Path) -> Path:
    path = workdir / "config" / "profiles" / "indoor_research_v0.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["params"]["warning_ttl_ms"] = WARNING_TTL_MS
    target = workdir / "config" / "profiles" / "privacy_short_ttl.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return target


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


@dataclass
class Observed:
    """Values issued during the run that must not persist anywhere."""

    tokens: list[str] = field(default_factory=list)
    responses: list[bytes] = field(default_factory=list)
    ws_errors: list[str] = field(default_factory=list)
    output_logs: list[str] = field(default_factory=list)

    def http_post(self, url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, payload = post_json(url, body)
        for key in ("token", "refresh_token"):
            if isinstance(payload.get(key), str):
                self.tokens.append(payload[key])
        return status, payload


def client(live: LiveServer, observed: Observed, **overrides: Any) -> GlassClient:
    cls = overrides.pop("cls", GlassClient)
    config = ClientConfig(
        server_url=live.url,
        device_id=overrides.pop("device", "dev_example"),
        secret=overrides.pop("secret", SECRET),
        inputs=overrides.pop("inputs", ("camera_0",)),
        outputs=("hud", "speaker"),
        backoff_s=FAST_BACKOFF,
    )
    instance: GlassClient = cls(config, http_post=observed.http_post)
    return instance


async def exercise(live: LiveServer, observed: Observed) -> None:
    # 1) Warning -> alert played; malformed, reserved and binary input; reconnect with rotation.
    glass = client(live, observed)
    await glass.start()
    await asyncio.wait_for(glass.connected.wait(), 5)
    for seq in range(4):
        await glass.send_observation(obstacle(seq))
        await asyncio.sleep(0.1)
    await until(lambda: glass.outputs.count("ack_played") == 1)
    ws = glass._ws
    assert ws is not None
    await ws.send(json.dumps({"type": "observation", "observation": {"note": SCHEMA_CANARY}}))
    frame_b64 = base64.b64encode(RAW_CANARY).decode("ascii")
    await ws.send(json.dumps({"type": "frame", "jpeg": frame_b64, "tag": FRAME_CANARY}))
    await ws.send(RAW_CANARY)
    await glass.report_health("camera_0", "down", DETAIL_CANARY)
    await until(lambda: len(glass.errors) >= 2)  # rejected_schema + unsupported (x2)
    observed.ws_errors += [e.model_dump_json() for e in glass.errors]
    await glass.drop_connection()
    await until(lambda: len(glass.sync_sent) == 2)  # refresh rotated both tokens
    assert glass._credentials is not None
    async with httpx.AsyncClient(trust_env=False) as http:
        status = await http.get(
            f"{live.url}/v1/sessions/{glass.session_id}/status",
            headers={"Authorization": f"Bearer {glass._credentials.token}"},
        )
        observed.responses.append(status.content)
        # REST with a secret in a wrong and in a malformed request.
        for body in (
            {
                "device_id": "dev_example",
                "secret": WRONG_SECRET,
                "capabilities": {"inputs": [], "outputs": ["hud"]},
            },
            {"device_id": "dev_example", "secret": WRONG_SECRET, "capabilities": "x"},
        ):
            response = await http.post(f"{live.url}/v1/sessions", json=body)
            assert response.status_code in (400, 401)
            observed.responses.append(response.content)
    observed.output_logs += [json.dumps(e.as_dict()) for e in glass.outputs.events]
    await glass.stop()

    # 2) OCR / ASR text from the insecure dev device (all capabilities).
    reader = client(live, observed, device="dev_fixture", secret=None, inputs=("camera_0", "mic_0"))
    await reader.start()
    await asyncio.wait_for(reader.connected.wait(), 5)
    now = time.monotonic_ns()
    await reader.send_observation(
        ObservationDraft(
            stream_id="camera_0",
            seq=0,
            capture_ts_monotonic_ns=now,
            payload=OcrPayload(
                kind="ocr_text",
                frame_id=f"{FRAME_CANARY}-ocr",
                text=OCR_CANARY,
                bbox=(0.1, 0.1, 0.5, 0.2),
                language="ko",
                score=0.9,
            ),
        )
    )
    await reader.send_observation(
        ObservationDraft(
            stream_id="mic_0",
            seq=0,
            capture_ts_monotonic_ns=now,
            payload=AsrPayload(
                kind="asr_transcript",
                utterance_id="utt_1",
                revision=0,
                is_final=True,
                text=ASR_CANARY,
                speaker_role="untrusted_environment",
                authentication_basis="none",
            ),
        )
    )
    await reader.send_observation(
        ObservationDraft(
            stream_id="mic_0",
            seq=1,
            capture_ts_monotonic_ns=now,
            payload=HealthPayload(kind="health", status="degraded", detail_code=DETAIL_CANARY),
        )
    )
    await asyncio.sleep(0.3)
    observed.output_logs += [json.dumps(e.as_dict()) for e in reader.outputs.events]
    await reader.stop()

    # 3) Unacknowledged alert expires (unknown_delivery); a late REST ACK is logged.
    silent = client(live, observed, cls=SilentClient)
    await silent.start()
    await asyncio.wait_for(silent.connected.wait(), 5)
    for seq in range(3):
        await silent.send_observation(obstacle(seq))
        await asyncio.sleep(0.1)
    await until(lambda: silent.outputs.hud_alert_id is not None)
    alert_id = silent.outputs.hud_alert_id
    await asyncio.sleep(WARNING_TTL_MS / 1000 + 0.3)
    assert silent._credentials is not None
    async with httpx.AsyncClient(trust_env=False) as http:
        response = await http.post(
            f"{live.url}/v1/alerts/{alert_id}/ack",
            json={"status": "played"},
            headers={"Authorization": f"Bearer {silent._credentials.token}"},
        )
    assert response.json()["delivery_state"] == "played"
    observed.responses.append(response.content)
    observed.output_logs += [json.dumps(e.as_dict()) for e in silent.outputs.events]
    await silent.stop()


@pytest.mark.parametrize(
    "mode",
    [
        "uvicorn_info",  # uvicorn default logging
        "uvicorn_debug",  # `uvicorn ... --log-level debug`: no frame dumps
        "server_main",  # the `python -m voa.server` log config (JSON allowlist)
        "server_main_debug",
    ],
)
def test_e2e_run_leaves_no_secret_token_or_payload_text(
    mode: str, tmp_path: Path, capfd: pytest.CaptureFixture[str], records: RecordCollector
) -> None:
    workdir = make_workdir(tmp_path / "server")
    live = LiveServer(
        workdir,
        {"VOA_POLICY_PROFILE": str(short_ttl_profile(workdir))},
        log_level="debug" if mode.endswith("debug") else "info",
        log_config=server_log_config() if mode.startswith("server_main") else None,
    )
    observed = Observed()
    live.start()
    records.attach()
    try:
        asyncio.run(exercise(live, observed))
    finally:
        live.stop()  # closes the DB so WAL content is flushed before the scan
    out, err = capfd.readouterr()

    leaks = Leaks()
    for name, value in {
        "secret": SECRET,
        "wrong_secret": WRONG_SECRET,
        "alert_text": ALERT_TEXT,
        "frame_id": FRAME_CANARY,
        "ocr_text": OCR_CANARY,
        "asr_text": ASR_CANARY,
        "detail_code": DETAIL_CANARY,
        "schema_input": SCHEMA_CANARY,
    }.items():
        leaks.watch(name, value)
    leaks.watch("raw_bytes", RAW_CANARY)
    leaks.watch("raw_b64", base64.b64encode(RAW_CANARY).decode("ascii"))
    assert len(observed.tokens) >= 8  # 3 sessions x 2 + one refresh x 2
    for index, token in enumerate(observed.tokens):
        leaks.watch(f"token_{index}", token)

    # Config is test input (example secret comment, warning text in the profile), not output.
    # var/config holds the missions imported from those profiles (ADR-0010): input as well.
    files = leaks.scan_tree(tmp_path, skip=[workdir / "config", workdir / "var" / "config"])
    assert any(path.name == "voa.sqlite3" for path in files)
    leaks.scan("stdout", out.encode("utf-8"))
    leaks.scan("stderr", err.encode("utf-8"))
    for index, dump in enumerate(records.dumps):
        leaks.scan(f"log_record_{index}", dump.encode("utf-8"))
    for index, body in enumerate(observed.responses):
        leaks.scan(f"rest_response_{index}", body)
    for index, text in enumerate(observed.ws_errors + observed.output_logs):
        leaks.scan(f"client_view_{index}", text.encode("utf-8"))

    # The run really produced logs and rows, so "0 found" is not vacuous.
    assert any("late_ack" in dump for dump in records.dumps)
    if mode.startswith("server_main"):
        lines = [line for line in err.splitlines() if line.strip()]
        assert lines and all(isinstance(json.loads(line), dict) for line in lines)
        assert '"event":"late_ack"' in err
    assert leaks.found == [], leaks.found
