"""Console + rotating-file logging setup.

P0-1: every handler runs ``_RedactFilter`` so child loggers cannot bypass it.
A logger-level filter does not cover records handled only by parent handlers.

P2-22: several processes share this log file. ``RotatingFileHandler`` rotates
inside each process, so concurrent writers can drop lines or write past a
rename. Default stays rotating so existing units keep their file logs.
Set ``LOG_HANDLER=stdout`` under systemd (journald owns the stream) or
``LOG_HANDLER=watched`` when an external logrotate drives a ``WatchedFileHandler``.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler, WatchedFileHandler
from pathlib import Path

from config import LOG_BACKUP_COUNT, LOG_DIR, LOG_FILE, LOG_LEVEL, LOG_MAX_BYTES

_CONFIGURED = False

# api-key= / api_key= and Telegram /bot<token> path segments.
_SECRET_RE = re.compile(r"(?i)(api[-_]?key=|/bot)[^&\s'\"/]+")


def redact_secrets(text: str) -> str:
    """Same rule as the handler filter. Use before logs and alert messages."""
    return _SECRET_RE.sub(r"\1<REDACTED>", text)


class _RedactFilter(logging.Filter):
    """Handler-level redaction. Mutates the record so formatters never see secrets."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = redact_secrets(msg)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


def _file_handler(log_path: Path, level: int, fmt: logging.Formatter) -> logging.Handler | None:
    """Build the file handler, or None when stdout-only is requested."""
    mode = os.environ.get("LOG_HANDLER", "rotating").strip().lower()
    if mode in ("stdout", "off", "none", "journal"):
        return None
    if mode == "watched":
        handler: logging.Handler = WatchedFileHandler(log_path, encoding="utf-8")
    else:
        # rotating (default). See module docstring for the multi-process race.
        handler = RotatingFileHandler(
            log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    handler.setLevel(level)
    handler.setFormatter(fmt)
    handler.addFilter(_RedactFilter())
    return handler


def setup_logging(level: str | None = None) -> logging.Logger:
    """Configure root crawler logger once; return package logger."""
    global _CONFIGURED
    logger = logging.getLogger("dexscreener")
    if _CONFIGURED:
        return logger

    log_level = getattr(logging, (level or LOG_LEVEL).upper(), logging.INFO)
    logger.setLevel(log_level)
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(fmt)
    console.addFilter(_RedactFilter())
    logger.addHandler(console)

    log_dir = Path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = _file_handler(log_dir / LOG_FILE, log_level, fmt)
    if file_handler is not None:
        logger.addHandler(file_handler)

    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    if name:
        return logging.getLogger(f"dexscreener.{name}")
    return logging.getLogger("dexscreener")
