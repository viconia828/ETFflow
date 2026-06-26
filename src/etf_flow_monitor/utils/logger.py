"""Small logging wrapper."""

from __future__ import annotations

import logging
from typing import TextIO


DEFAULT_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_LOGGER_CONFIGURED = False


def _normalize_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    name = str(level).strip().upper()
    if not name:
        return logging.INFO
    return getattr(logging, name, logging.INFO)


def configure_logging(level: int | str = logging.INFO, *, stream: TextIO | None = None, force: bool = False) -> None:
    """Configure process-level logging once unless force=True."""
    global _LOGGER_CONFIGURED
    if _LOGGER_CONFIGURED and not force:
        return
    logging.basicConfig(level=_normalize_level(level), format=DEFAULT_LOG_FORMAT, stream=stream, force=force)
    _LOGGER_CONFIGURED = True


def get_logger(name: str = "etf_flow_monitor") -> logging.Logger:
    if not _LOGGER_CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
