"""Daemon logging spine — rotated, ISO-8601-timestamped, manifest-configurable.

The interactive CLI commands (``status`` / ``kpi`` / ``canary`` / ...) render to
a rich console and are unaffected by this module. The **daemon** path
(``trading-bot start [--serve]``) is different: it runs headless for days, so its
story has to land somewhere durable. :func:`configure_daemon_logging` wires the
standard-library :mod:`logging` root logger — driven by the manifest's optional
``logging:`` section (:class:`~trading_bot.application.config.LoggingConfig`) — to

* a :class:`~logging.handlers.TimedRotatingFileHandler` that rolls
  ``<dir>/daemon.log`` at **midnight** and keeps ``retention_days`` dated
  backups, and
* a :class:`~logging.StreamHandler` on ``stderr`` (so a systemd/journal unit —
  or a plain ``nohup`` redirect — still captures the same lines).

Both share a formatter whose timestamp is **local time with a numeric UTC
offset** (e.g. ``2026-07-14T13:35:40.123+02:00``): unambiguous across a DST
change and directly sortable/greppable. The engine's own namespaces log at the
configured ``level`` (INFO by default); the noisy third-party namespaces
(:data:`NOISY_NAMESPACES`) are capped at ``WARNING`` so INFO stays the engine's
story (an APScheduler *misfire* WARNING still flows).

The setup is **idempotent**: it tags the handlers it installs
(:data:`OWNED_HANDLER_ATTR`) and removes its own stale handlers on a re-call, so
a second invocation never double-writes and never disturbs handlers attached by
anything else (pytest, an embedding host, ...).

Secrets discipline: the formatter adds nothing beyond time / level / logger name
/ message — no request bodies, no credentials. Callers stay responsible for never
passing a credential-adjacent value into a log record.
"""

from __future__ import annotations

# Built-in
import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

# Local
from trading_bot.application.config import LoggingConfig

__all__ = [
    "configure_daemon_logging",
    "OWNED_HANDLER_ATTR",
    "NOISY_NAMESPACES",
]

#: Attribute stamped ``True`` on every handler this module installs, so a re-call
#: can find and drop exactly its own handlers (idempotence) without touching any
#: handler attached by something else.
OWNED_HANDLER_ATTR = "_trading_bot_daemon_owned"

#: Third-party logger namespaces capped at ``WARNING`` so INFO stays the engine's
#: story rather than framework chatter. An APScheduler misfire WARNING (a real
#: signal the daemon fell behind) is >= WARNING, so it still reaches the file.
NOISY_NAMESPACES: tuple[str, ...] = (
    "apscheduler",
    "uvicorn",
    "uvicorn.access",
    "httpx",
    "websockets",
)

#: The shared log-line layout: ISO timestamp, padded level, logger name, message.
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"


class _OffsetIsoFormatter(logging.Formatter):
    """A :class:`logging.Formatter` whose ``asctime`` is local ISO-8601 with offset.

    Overrides :meth:`logging.Formatter.formatTime` to render the record's
    creation time as local time carrying a **numeric UTC offset** (e.g.
    ``2026-07-14T13:35:40.123+02:00``), millisecond precision. The offset is what
    makes a line unambiguous across a DST change; ``datefmt`` is ignored on
    purpose so every line is uniform.
    """

    def formatTime(  # noqa: N802 - overriding the stdlib camelCase hook
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        """Return the record's local creation time as ISO-8601 with numeric offset."""
        # A naive local datetime, then `.astimezone()` attaches the local offset.
        return (
            datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds")
        )


def configure_daemon_logging(cfg: LoggingConfig) -> None:
    """Wire the root logger for the daemon: rotated file + stderr, per ``cfg``.

    Attaches an owned :class:`~logging.handlers.TimedRotatingFileHandler` (rolls
    ``cfg.dir / "daemon.log"`` at midnight, keeps ``cfg.retention_days`` backups)
    and an owned :class:`~logging.StreamHandler` on ``stderr`` to the root logger,
    both using the :class:`_OffsetIsoFormatter`; sets the root level to
    ``cfg.level`` and caps :data:`NOISY_NAMESPACES` at ``WARNING``. Creates
    ``cfg.dir`` if missing.

    Idempotent: any handler this module previously installed (tagged
    :data:`OWNED_HANDLER_ATTR`) is removed and closed first, so a second call
    leaves exactly one owned handler of each kind — no double-writing — and
    handlers owned by anything else are left untouched.

    Parameters
    ----------
    cfg : LoggingConfig
        The resolved ``logging:`` section (level / directory / retention).

    Returns
    -------
    None

    """
    cfg.dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()

    # Idempotence: drop (and close) only the handlers this module installed
    # before, so a re-call never accumulates duplicates.
    for handler in list(root.handlers):
        if getattr(handler, OWNED_HANDLER_ATTR, False):
            root.removeHandler(handler)
            handler.close()

    formatter = _OffsetIsoFormatter(_LOG_FORMAT)

    file_handler = TimedRotatingFileHandler(
        cfg.dir / "daemon.log",
        when="midnight",
        backupCount=cfg.retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    setattr(file_handler, OWNED_HANDLER_ATTR, True)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    setattr(stream_handler, OWNED_HANDLER_ATTR, True)
    root.addHandler(stream_handler)

    # `cfg.level` is a validated, upper-cased level name — `setLevel` accepts it.
    root.setLevel(cfg.level)

    for namespace in NOISY_NAMESPACES:
        logging.getLogger(namespace).setLevel(logging.WARNING)
