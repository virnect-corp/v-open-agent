"""Server logging: JSON allowlist lines and a level floor (SPEC 8.2, DAT-02).

`python -m voa.server` hands `server_log_config()` to uvicorn so every server
logger writes through `JsonLogFormatter`: free-form messages, arguments and
tracebacks are never written, only allowlisted structured fields. Records
without a structured `event` get the logger name, so a line still says where
it came from. Access logs are off (they would only be `{"event": ...}` noise).

uvicorn passes the `uvicorn.error` logger to the websockets protocol, which
dumps every frame (tokens, payloads) at DEBUG. `pin_log_levels()` runs at app
start and raises server loggers to at least INFO, so even
`uvicorn voa.server.app:app --log-level debug` cannot enable those dumps.
"""

import logging
from typing import Any

SERVER_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "websockets",
    "voa",
)
MIN_LEVEL = logging.INFO


class LoggerEventFilter(logging.Filter):
    """Give unstructured records `event = <logger name>`; structured events are kept."""

    def filter(self, record: logging.LogRecord) -> bool:
        if type(record.__dict__.get("event")) is not str:
            record.__dict__["event"] = record.name
        return True


def server_log_config() -> dict[str, Any]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": "voa.storage.logging.JsonLogFormatter"}},
        "filters": {"event": {"()": "voa.server.logconfig.LoggerEventFilter"}},
        "handlers": {
            "stderr": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "json",
                "filters": ["event"],
            }
        },
        "loggers": {
            name: {"handlers": ["stderr"], "level": "INFO", "propagate": False}
            for name in SERVER_LOGGERS
        },
    }


def pin_log_levels() -> None:
    """Raise server loggers to at least INFO (NOTSET counts as below); never lower them."""
    for name in SERVER_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(max(logger.level, MIN_LEVEL))
