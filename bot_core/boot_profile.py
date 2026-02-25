from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_logger: logging.Logger | None = None


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("BootProfile")
    logger.setLevel(logging.INFO)

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_path = log_dir / "boot_profile.log"

    handler = RotatingFileHandler(
        file_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False

    _logger = logger
    return logger


def log_event(step: str, duration: float, detail: str | None = None) -> None:
    """Write a single boot profiling entry."""
    logger = _get_logger()
    msg = f"{step} {duration:.3f}s"
    if detail:
        msg += f" | {detail}"
    logger.info(msg)


class Span:
    """Lightweight context for measuring a single step."""

    def __init__(self, step: str, detail: str | None = None):
        self.step = step
        self.detail = detail
        self._start = time.perf_counter()

    def finish(self, detail: str | None = None) -> None:
        end = time.perf_counter()
        log_event(self.step, end - self._start, detail or self.detail)


def measure(step: str, detail: str | None = None) -> Span:
    """Create a span; call .finish() to record."""
    return Span(step, detail)
