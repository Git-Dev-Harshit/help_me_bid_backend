"""Structured logging.

Emits newline-delimited JSON in production (easy to ship to any log backend)
and a compact human-readable line locally.  Arbitrary key/value context is
attached via the ``extra`` argument::

    logger.info("scrape.completed", extra={"inserted": 3, "updated": 12})

A redaction filter strips values that must never reach the logs (passwords,
tokens, push tokens, auth headers) even if a caller passes them by accident.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any

from app.core.config import settings

# Attributes present on every LogRecord; anything else was supplied by the
# caller through ``extra`` and therefore belongs in the structured payload.
_RESERVED_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)

SENSITIVE_KEYS = frozenset(
    {
        "password", "new_password", "current_password", "hashed_password",
        "password_hash", "token", "access_token", "refresh_token", "jwt",
        "push_token", "authorization", "api_key", "secret", "jwt_secret_key",
        "credentials", "vapid_private_key",
    }
)

REDACTED = "***redacted***"


def _redact(key: str, value: Any) -> Any:
    return REDACTED if key.lower() in SENSITIVE_KEYS else value


class RedactionFilter(logging.Filter):
    """Blank out sensitive values attached to a record via ``extra``."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__):
            if key not in _RESERVED_ATTRS and key.lower() in SENSITIVE_KEYS:
                record.__dict__[key] = REDACTED
        return True


def _record_context(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: _redact(key, value)
        for key, value in record.__dict__.items()
        if key not in _RESERVED_ATTRS and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(
                record.created, tz=dt.UTC
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(_record_context(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Readable single-line output for local development."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = dt.datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        context = _record_context(record)
        suffix = ""
        if context:
            suffix = " " + " ".join(f"{k}={v}" for k, v in context.items())
        line = f"{stamp} {record.levelname:<7} {record.name:<28} {record.getMessage()}{suffix}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging() -> None:
    """Install the root logging configuration. Safe to call more than once."""
    formatter: logging.Formatter = (
        JsonFormatter() if settings.log_format == "json" else ConsoleFormatter()
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Uvicorn ships its own handlers; route them through ours instead so every
    # line in the container shares one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # These are chatty at INFO and say nothing useful in normal operation.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger."""
    return logging.getLogger(name)
