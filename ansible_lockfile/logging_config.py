"""Colored stderr logging for interactive CLI use."""

from __future__ import annotations

import logging
import os
import sys


# ANSI SGR sequences (level name only; message stays plain for copy/paste).
_RESET = "\033[0m"
_LEVEL_COLORS = {
    logging.DEBUG: "\033[36m",  # cyan
    logging.INFO: "\033[32m",  # green
    logging.WARNING: "\033[33m",  # yellow
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}


def use_color(*, stream=None) -> bool:
    """True when we should emit ANSI colors (TTY + no NO_COLOR)."""
    if os.environ.get("NO_COLOR", ""):
        return False
    if os.environ.get("FORCE_COLOR", ""):
        return True
    target = stream if stream is not None else sys.stderr
    return hasattr(target, "isatty") and target.isatty()


class ColorFormatter(logging.Formatter):
    """Format ``LEVEL: message`` with an optional colored level name."""

    def __init__(self, *, color: bool = False) -> None:
        super().__init__(fmt="%(levelname)s: %(message)s")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        if not self.color:
            return super().format(record)
        original = record.levelname
        color = _LEVEL_COLORS.get(record.levelno)
        if color:
            record.levelname = f"{color}{original}{_RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original


def configure_logging(*, debug: bool = False, stream=None) -> None:
    """Configure root logging once for the CLI."""
    target = stream if stream is not None else sys.stderr
    handler = logging.StreamHandler(target)
    handler.setFormatter(ColorFormatter(color=use_color(stream=target)))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
