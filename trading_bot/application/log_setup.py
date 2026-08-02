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
passing a credential-adjacent value into a log record — with one exception this
module handles itself: uvicorn's *access* logger writes the request URL verbatim,
so the dashboard's documented ``?token=`` script auth would land in the journal on
every request. :func:`install_access_log_redaction` scrubs it at the source (see
:class:`AccessLogRedactionFilter`).
"""

from __future__ import annotations

# Built-in
import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

# Local
from trading_bot.application.config import LoggingConfig
from trading_bot.transport.http import redact_url

__all__ = [
    "configure_daemon_logging",
    "install_access_log_redaction",
    "AccessLogRedactionFilter",
    "OWNED_HANDLER_ATTR",
    "NOISY_NAMESPACES",
    "ACCESS_LOG_NAMESPACES",
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

#: The uvicorn logger namespaces whose records can carry a request URL — and so a
#: query-string secret. ``uvicorn.access`` writes one line per request (the leak
#: that motivated this); ``uvicorn.error`` carries the lifecycle/exception lines,
#: which quote the URL on a failed request. Both are scrubbed by
#: :func:`install_access_log_redaction`.
ACCESS_LOG_NAMESPACES: tuple[str, ...] = ("uvicorn.access", "uvicorn.error")

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


class AccessLogRedactionFilter(logging.Filter):
    """Scrub query-string secrets out of uvicorn's request log lines.

    uvicorn's access logger formats one record per request as
    ``'%s - "%s %s HTTP/%s" %d'`` with the **raw request target** among its
    ``args`` — so the dashboard's documented ``?token=…`` script auth is written
    verbatim to stderr/journald on every request. This filter rewrites the record
    in place before any handler formats it, applying
    :func:`~trading_bot.transport.http.redact_url` to ``record.msg`` and to every
    string in ``record.args``; the value of a sensitive parameter (``token``,
    ``signature``, ``api_key`` / ``apiKey``, ``nonce``, case-insensitively)
    becomes ``<redacted>``.

    Reusing the transport's scrubber is deliberate: one key set, one marker, so a
    URL is masked identically wherever it could reach a log. That function is a
    no-op on a string with no query part, so the record's other args (the client
    address, ``GET``, the HTTP version, the status code) pass through untouched,
    as does a query carrying nothing sensitive (``?symbol=BTCUSDT``).

    Never drops a record: :meth:`filter` always returns ``True``. It is a
    *sanitiser*, not a gate — a suppressed access line would cost observability,
    which is not the trade being made here.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact *record* in place; always keep it.

        Parameters
        ----------
        record : logging.LogRecord
            The record about to be handled. Mutated in place — filters run once
            per record before formatting, so every handler downstream (file,
            stderr, journald) sees the scrubbed version.

        Returns
        -------
        bool
            Always ``True`` — the record is sanitised, never suppressed.
        """
        if isinstance(record.msg, str):
            record.msg = redact_url(record.msg)
        # Only tuple args are positional `%s` substitutions worth scrubbing; a
        # mapping-style `record.args` (`%(key)s` formatting) is left alone rather
        # than rebuilt — uvicorn never uses it, and mutating an unknown mapping
        # shape risks corrupting a third-party record.
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_url(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True


def install_access_log_redaction() -> None:
    """Attach one :class:`AccessLogRedactionFilter` to each uvicorn log namespace.

    Call this **before** handing control to uvicorn (``uvicorn.run`` /
    ``Server.serve``) on every serving path, so no request line can be emitted
    unscrubbed. The filters go on the *loggers* named in
    :data:`ACCESS_LOG_NAMESPACES`, not on handlers, and that placement is
    load-bearing: uvicorn configures logging at startup with
    :func:`logging.config.dictConfig` (its ``LOGGING_CONFIG``), which replaces a
    configured logger's **handlers** but leaves filters attached
    programmatically to the logger itself in place. A handler-level filter would
    be discarded with the handler it sat on; a logger-level one survives, and
    runs once per record before any handler formats it.

    Idempotent: a namespace that already carries a filter of this class is left
    alone, so repeated calls in one process (three CLI serving commands, or a
    test suite invoking them many times) never stack duplicate filters on the
    process-global uvicorn loggers.

    Returns
    -------
    None

    """
    for namespace in ACCESS_LOG_NAMESPACES:
        logger = logging.getLogger(namespace)
        if any(isinstance(f, AccessLogRedactionFilter) for f in logger.filters):
            continue
        logger.addFilter(AccessLogRedactionFilter())
