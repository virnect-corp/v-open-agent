"""`python -m voa.server`: validate settings, then start uvicorn (SPEC 8.3).

Invalid settings refuse start with exit code 2 and name only the variables.
In the pilot profile the validated TLS cert/key are passed to uvicorn.
Logging uses the JSON allowlist config (`voa.server.logconfig`).
"""

import os
import sys
from pathlib import Path
from typing import Any

import uvicorn

from voa.server.logconfig import server_log_config
from voa.server.settings import Settings, SettingsError

APP = "voa.server.app:app"


def uvicorn_options(settings: Settings) -> dict[str, Any]:
    options: dict[str, Any] = {
        "host": settings.bind,
        "port": settings.port,
        "log_config": server_log_config(),  # JSON allowlist lines (SPEC 8.2)
        "access_log": False,
    }
    if settings.profile == "pilot":
        options["ssl_certfile"] = str(settings.tls_cert)
        options["ssl_keyfile"] = str(settings.tls_key)
    return options


def main() -> int:
    try:
        settings = Settings.from_env(os.environ, base_dir=Path.cwd())
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    uvicorn.run(APP, **uvicorn_options(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
