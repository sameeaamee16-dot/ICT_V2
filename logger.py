from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler

from config import LOG_DIR

_CONFIGURED = False


class SafeRotatingFileHandler(RotatingFileHandler):
    """Rotates logs, but keeps writing if Windows has the file locked."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._rollover_blocked_until = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if time.monotonic() < self._rollover_blocked_until:
            return False
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            self._rollover_blocked_until = time.monotonic() + 60
            if self.stream is None or self.stream.closed:
                self.stream = self._open()


def _configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    if root.handlers:
        _CONFIGURED = True
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = SafeRotatingFileHandler(
        LOG_DIR / "terminal.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_logging()
    logger = logging.getLogger(name)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    return logger
