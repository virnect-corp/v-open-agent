"""A real uvicorn server on 127.0.0.1 for E2E tests (loopback works with `network_mode: none`)."""

import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from voa.server.app import create_app
from voa.server.settings import Settings

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
SCENARIOS = SERVER.parents[1] / "contracts" / "fixtures" / "scenarios"
SECRET = "example-secret-change-me"  # devices.example.yaml test secret
HEARTBEAT_MS = 100
WATCHDOG_MS = 300


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_workdir(workdir: Path) -> Path:
    """Copy profiles, model manifests and the example device file under `workdir/config`."""
    shutil.copytree(CONFIG / "profiles", workdir / "config" / "profiles")
    shutil.copytree(CONFIG / "models", workdir / "config" / "models")
    shutil.copy(CONFIG / "devices.example.yaml", workdir / "config" / "devices.yaml")
    return workdir


class LiveServer:
    """uvicorn in a thread; `start` after `stop` brings up fresh process state on the same port."""

    def __init__(
        self,
        workdir: Path,
        env: dict[str, str] | None = None,
        *,
        log_level: str = "info",
        log_config: dict[str, Any] | None = None,
    ) -> None:
        self.port = free_port()
        self.log_level = log_level
        self.log_config = log_config
        self.settings = Settings.from_env(
            {
                "VOA_HEARTBEAT_MS": str(HEARTBEAT_MS),
                "VOA_CLIENT_WATCHDOG_MS": str(WATCHDOG_MS),
                "VOA_DB_PATH": str(workdir / "voa.sqlite3"),
                **(env or {}),
            },
            base_dir=workdir,
        )
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        app = create_app(lambda: self.settings)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level=self.log_level,
            log_config=LOGGING_CONFIG if self.log_config is None else self.log_config,
            lifespan="on",
            timeout_graceful_shutdown=1,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
            time.sleep(0.01)
        self._server, self._thread = server, thread

    def stop(self) -> None:
        if self._server is not None and self._thread is not None:
            self._server.should_exit = True
            self._thread.join(timeout=10)
        self._server = self._thread = None
