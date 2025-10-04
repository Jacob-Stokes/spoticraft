"""Logging utilities shared across CLI commands and background workers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

import structlog


_DEFAULT_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.stdlib.add_log_level,
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def configure_logging(
    level: str = "INFO",
    json_output: bool = False,
    log_file: Optional[Union[str, Path]] = None,
) -> None:
    """Initialise structlog and stdlib logging.

    Parameters
    ----------
    level:
        Textual logging level (e.g. ``"DEBUG"``). Defaults to ``"INFO"``.
    json_output:
        Emit JSON-structured logs when ``True`` for machine consumption.
    """

    handlers = [logging.StreamHandler()]

    if log_file:
        log_path = Path(log_file).expanduser()
        if not log_path.parent.exists():
            log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(level=level, format="%(message)s", handlers=handlers, force=True)

    processors = list(_DEFAULT_PROCESSORS)
    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Keep third-party clients quieter unless debugging explicitly.
    spotipy_level = logging.INFO if level.upper() == "DEBUG" else logging.WARNING
    for logger_name in ("spotipy", "spotipy.client"):
        logging.getLogger(logger_name).setLevel(spotipy_level)


def get_logger(name: Optional[str] = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to ``name``.

    A default logger name is derived from the caller if ``name`` is omitted.
    """

    return structlog.get_logger(name)
