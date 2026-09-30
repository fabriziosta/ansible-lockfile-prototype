from __future__ import annotations

import io
import logging

from ansible_lockfile.logging_config import ColorFormatter, configure_logging, use_color


def test_use_color_respects_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert use_color(stream=io.StringIO()) is False


def test_use_color_force(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert use_color(stream=io.StringIO()) is True


def test_color_formatter_colors_level_name():
    fmt = ColorFormatter(color=True)
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="checksum mismatch",
        args=(),
        exc_info=None,
    )
    out = fmt.format(record)
    assert "\033[33mWARNING\033[0m: checksum mismatch" == out
    assert record.levelname == "WARNING"


def test_color_formatter_plain_when_disabled():
    fmt = ColorFormatter(color=False)
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ok",
        args=(),
        exc_info=None,
    )
    assert fmt.format(record) == "INFO: ok"


def test_configure_logging_emits_to_stream(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = io.StringIO()
    configure_logging(debug=False, stream=stream)
    logging.getLogger("ansible_lockfile.test").error("boom")
    assert "\033[31mERROR\033[0m: boom" in stream.getvalue()
