from __future__ import annotations

import logging
import os

DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVEL_ENV = "NEWSRAG_LOG_LEVEL"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
_ALLOWED_LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


class LoggingConfigError(ValueError):
    """Raised when runtime logging configuration is invalid."""


def resolve_log_level(value: str | None = None) -> int:
    """Resolve a standard logging level from an explicit or environment value."""

    configured = value if value is not None else os.environ.get(LOG_LEVEL_ENV, DEFAULT_LOG_LEVEL)
    normalized = configured.strip().upper()
    try:
        return _ALLOWED_LOG_LEVELS[normalized]
    except KeyError as exc:
        allowed = ", ".join(_ALLOWED_LOG_LEVELS)
        raise LoggingConfigError(
            f"Invalid {LOG_LEVEL_ENV} value {configured!r}; expected one of: {allowed}"
        ) from exc


def configure_logging(value: str | None = None) -> int:
    """Configure application console logging and return the resolved level."""

    level = resolve_log_level(value)
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        force=True,
    )
    return level
