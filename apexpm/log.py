"""Logging that never breaks the bot.

Rule: a logging failure must not take trading down.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_LOGGER = None
_FILE_PATH = None
_FMT = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")


def get_logger(log_path: str = None, name: str = "apexpm") -> logging.Logger:
    """Shared logger. Modules may call this at import time without a path; the
    first call that passes a path attaches the rotating file handler."""
    global _LOGGER, _FILE_PATH
    if _LOGGER is None:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(_FMT)
        logger.addHandler(stream)
        _LOGGER = logger
    if log_path and _FILE_PATH is None:
        try:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5)
            fh.setFormatter(_FMT)
            _LOGGER.addHandler(fh)
            _FILE_PATH = log_path
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("file logging disabled: %s", exc)
    return _LOGGER


def safe(fn, *args, **kwargs):
    """Call a logging-ish function and swallow any error."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return None
