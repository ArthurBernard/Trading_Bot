"""Tests for :func:`~trading_bot.application.log_setup.configure_daemon_logging`.

These prove the daemon logging spine wires as intended: the root logger gains
exactly one owned rotated-file handler and one owned stderr handler, a second
call does not double them (idempotence), each line is ISO-8601 with a numeric UTC
offset, the configured level reaches the file while the noisy third-party
namespaces are capped at WARNING, and the log directory/file are created under
the manifest-declared ``dir``.

Test hygiene: :func:`configure_daemon_logging` mutates *process-global* logging
state (the root logger's handlers/level and a few namespace levels). The
``restore_root_logging`` fixture snapshots and restores all of it around every
test so the wider suite's own logging is never polluted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

from trading_bot.application.config import LoggingConfig
from trading_bot.application.log_setup import (
    NOISY_NAMESPACES,
    OWNED_HANDLER_ATTR,
    configure_daemon_logging,
)

#: A regex for one emitted line: ISO-8601 local time with a numeric UTC offset
#: (e.g. ``2026-07-14T13:35:40.123+02:00``) followed by a space then the level.
_ISO_OFFSET_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} "
)


@pytest.fixture
def restore_root_logging() -> Iterator[None]:
    """Snapshot and restore the process-global logging state around a test.

    Saves the root logger's handlers + level and every capped-namespace level,
    then on teardown removes/closes any handler the test installed and puts the
    original handlers, root level and namespace levels back — so a test that
    wires the daemon logging never leaks handlers into the rest of the suite.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_ns_levels = {ns: logging.getLogger(ns).level for ns in NOISY_NAMESPACES}
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(saved_level)
        for ns, level in saved_ns_levels.items():
            logging.getLogger(ns).setLevel(level)


def _owned(kind: type[logging.Handler]) -> list[logging.Handler]:
    """The root logger's owned handlers whose *exact* type is ``kind``."""
    return [
        h
        for h in logging.getLogger().handlers
        if getattr(h, OWNED_HANDLER_ATTR, False) and type(h) is kind
    ]


def test_configure_wires_one_owned_file_and_stream_handler(
    tmp_path: Path, restore_root_logging: None
) -> None:
    """After configure, root has exactly one owned rotated-file + one stderr handler."""
    cfg = LoggingConfig(dir=tmp_path / "logs", retention_days=7)
    configure_daemon_logging(cfg)

    file_handlers = _owned(TimedRotatingFileHandler)
    stream_handlers = _owned(logging.StreamHandler)
    assert len(file_handlers) == 1
    assert len(stream_handlers) == 1

    fh = file_handlers[0]
    assert isinstance(fh, TimedRotatingFileHandler)
    assert fh.when == "MIDNIGHT"
    assert fh.backupCount == 7


def test_configure_is_idempotent(tmp_path: Path, restore_root_logging: None) -> None:
    """A second configure leaves exactly one owned handler of each kind (no doubling)."""
    cfg = LoggingConfig(dir=tmp_path / "logs")
    configure_daemon_logging(cfg)
    configure_daemon_logging(cfg)

    assert len(_owned(TimedRotatingFileHandler)) == 1
    assert len(_owned(logging.StreamHandler)) == 1


def test_formatter_is_iso_with_numeric_offset(
    tmp_path: Path, restore_root_logging: None
) -> None:
    """An emitted line is ISO-8601 with a numeric offset and carries level + name."""
    configure_daemon_logging(LoggingConfig(dir=tmp_path / "logs"))
    logging.getLogger("trading_bot.daemon.test").info("hello world")

    line = _last_log_line(tmp_path / "logs" / "daemon.log")
    assert _ISO_OFFSET_LINE.match(line), line
    assert "INFO" in line
    assert "trading_bot.daemon.test" in line
    assert "hello world" in line


def test_level_debug_reaches_file_and_noisy_namespaces_capped(
    tmp_path: Path, restore_root_logging: None
) -> None:
    """``level=DEBUG`` lets a DEBUG record reach the file; apscheduler stays WARNING."""
    configure_daemon_logging(LoggingConfig(dir=tmp_path / "logs", level="DEBUG"))

    logging.getLogger("trading_bot.daemon.test").debug("a debug line")
    contents = (tmp_path / "logs" / "daemon.log").read_text()
    assert "a debug line" in contents

    # The noisy namespaces are floored at WARNING regardless of the root level.
    assert logging.getLogger("apscheduler").level == logging.WARNING
    for ns in NOISY_NAMESPACES:
        assert logging.getLogger(ns).level == logging.WARNING


def test_default_level_info_drops_debug(
    tmp_path: Path, restore_root_logging: None
) -> None:
    """At the default INFO level, a DEBUG record does not reach the file."""
    configure_daemon_logging(LoggingConfig(dir=tmp_path / "logs"))

    logging.getLogger("trading_bot.daemon.test").debug("should-not-appear")
    logging.getLogger("trading_bot.daemon.test").info("should-appear")
    contents = (tmp_path / "logs" / "daemon.log").read_text()
    assert "should-not-appear" not in contents
    assert "should-appear" in contents


def test_log_dir_and_file_created(tmp_path: Path, restore_root_logging: None) -> None:
    """A missing (nested) ``dir`` is created and the log file appears on first emit."""
    log_dir = tmp_path / "nested" / "logs"
    assert not log_dir.exists()

    configure_daemon_logging(LoggingConfig(dir=log_dir))
    assert log_dir.is_dir()

    logging.getLogger("trading_bot.daemon.test").info("x")
    assert (log_dir / "daemon.log").is_file()


def _last_log_line(path: Path) -> str:
    """Flush the owned handlers and return the last non-empty line of ``path``."""
    for handler in logging.getLogger().handlers:
        if getattr(handler, OWNED_HANDLER_ATTR, False):
            handler.flush()
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert lines, f"no log lines written to {path}"
    return lines[-1]
