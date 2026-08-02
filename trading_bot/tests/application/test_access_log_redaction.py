"""Tests for the uvicorn access-log scrubber (:mod:`trading_bot.application.log_setup`).

The dashboard documents a ``?token=<secret>`` query parameter for script auth, and
uvicorn's access logger writes the request target **verbatim** — so before this
filter existed, every such request printed the live token to stderr/journald (it
did, on a real deploy). These tests pin the fix at the record level:

* a record shaped exactly like uvicorn's access line comes out with the token
  replaced by the ``<redacted>`` marker (percent-encoded by ``urlencode``, as the
  transport's own tests assert) and everything else (client address, method, HTTP
  version, status, the harmless ``x=1``) byte-identical;
* every sensitive key the transport knows about (``token`` / ``signature`` /
  ``api_key`` / ``apiKey`` / ``nonce``, case-insensitively) is masked, while a
  non-sensitive query and a query-less path pass through untouched;
* installing twice leaves exactly one filter per namespace (the CLI's three
  serving commands, and a test suite invoking them repeatedly, share one process);
* the filter **survives** uvicorn's own ``logging.config.dictConfig`` at startup —
  the load-bearing assumption behind installing at the *logger* level rather than
  on a handler.

Test hygiene: the uvicorn loggers are process-global, so the ``clean_uvicorn_loggers``
fixture strips every filter this module installs (and restores handlers/levels that
``dictConfig`` rewrites) after each test — the leak would otherwise silently change
what the rest of the suite logs.
"""

from __future__ import annotations

import logging
import logging.config
from collections.abc import Iterator

import pytest

from trading_bot.application.log_setup import (
    ACCESS_LOG_NAMESPACES,
    AccessLogRedactionFilter,
    install_access_log_redaction,
)

#: The uvicorn access-log record shape (uvicorn.logging.AccessFormatter's input):
#: client address, method, full request target, HTTP version, status code.
_ACCESS_MSG = '%s - "%s %s HTTP/%s" %d'


@pytest.fixture
def clean_uvicorn_loggers() -> Iterator[None]:
    """Restore the process-global uvicorn loggers around a test.

    Snapshots the filters, handlers, level and ``propagate`` flag of every
    namespace in :data:`ACCESS_LOG_NAMESPACES` (plus the ``uvicorn`` root, which
    uvicorn's ``LOGGING_CONFIG`` also rewrites) and puts them back on teardown, so
    neither an installed redaction filter nor a ``dictConfig`` call leaks into the
    rest of the suite.
    """
    names = ("uvicorn", *ACCESS_LOG_NAMESPACES)
    saved = {
        name: (
            list(logging.getLogger(name).filters),
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in names
    }
    yield
    for name, (filters, handlers, level, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.filters = filters
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _access_record(target: str) -> logging.LogRecord:
    """Build the access-log record uvicorn emits for a GET on *target*."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=_ACCESS_MSG,
        args=("127.0.0.1:5", "GET", target, "1.1", 200),
        exc_info=None,
    )


def test_filter_redacts_the_token_in_a_uvicorn_access_record() -> None:
    """The token vanishes from the rendered line; every other field survives.

    The exact leak seen in production: ``GET /api/health?token=<value> HTTP/1.1``
    written verbatim by the access logger.
    """
    record = _access_record("/api/health?token=SENTINEL&x=1")

    assert AccessLogRedactionFilter().filter(record) is True  # never drops a record

    message = record.getMessage()
    assert "SENTINEL" not in message
    # The marker as urlencode emits it (angle brackets percent-encoded) — the
    # bare substring "redacted" is what stays greppable either way, so the
    # assertion is written the way the transport's own tests write it.
    assert "token=%3Credacted%3E" in message or "token=<redacted>" in message
    assert "x=1" in message  # a harmless parameter stays readable
    # The rest of the access line is untouched — the log stays useful.
    assert '127.0.0.1:5 - "GET /api/health?token=' in message
    assert message.endswith(' HTTP/1.1" 200')


@pytest.mark.parametrize("key", ["token", "signature", "api_key", "apiKey", "nonce"])
def test_filter_redacts_every_sensitive_query_key(key: str) -> None:
    """All of the transport's sensitive keys are masked, whatever their casing.

    The filter delegates to the transport's scrubber precisely so the key set is
    shared: what is a secret on a signed broker URL is a secret in an access log.
    """
    record = _access_record(f"/api/health?{key}=SENTINEL")

    AccessLogRedactionFilter().filter(record)

    message = record.getMessage()
    assert "SENTINEL" not in message
    assert f"{key}=%3Credacted%3E" in message or f"{key}=<redacted>" in message


@pytest.mark.parametrize(
    "target", ["/api/strategies?symbol=BTCUSDT", "/api/health", "/"]
)
def test_filter_leaves_a_secret_free_request_byte_identical(target: str) -> None:
    """A non-sensitive query and a query-less path come through unchanged.

    Nothing about the ordinary access log changes — no re-quoting, no rewriting —
    so the scrubber is invisible until there is something to hide.
    """
    record = _access_record(target)
    expected = record.getMessage()

    AccessLogRedactionFilter().filter(record)

    assert record.getMessage() == expected


def test_install_is_idempotent(clean_uvicorn_loggers: None) -> None:
    """A second install adds no second filter — the CLI has three serving commands.

    They share one process (and one process-global logger), and the suite invokes
    them many times; stacked filters would redact repeatedly and grow without
    bound.
    """
    install_access_log_redaction()
    install_access_log_redaction()

    for namespace in ACCESS_LOG_NAMESPACES:
        installed = [
            f
            for f in logging.getLogger(namespace).filters
            if isinstance(f, AccessLogRedactionFilter)
        ]
        assert len(installed) == 1, namespace


def test_install_covers_both_uvicorn_namespaces(clean_uvicorn_loggers: None) -> None:
    """Both ``uvicorn.access`` (request lines) and ``uvicorn.error`` (which quotes
    the URL on a failed request) are covered."""
    install_access_log_redaction()

    assert set(ACCESS_LOG_NAMESPACES) == {"uvicorn.access", "uvicorn.error"}
    for namespace in ACCESS_LOG_NAMESPACES:
        assert any(
            isinstance(f, AccessLogRedactionFilter)
            for f in logging.getLogger(namespace).filters
        ), namespace


def test_filter_survives_uvicorns_dictconfig(clean_uvicorn_loggers: None) -> None:
    """Installing *before* ``uvicorn.run`` outlives uvicorn's own logging setup.

    This locks the reason the filter goes on the logger and not on a handler:
    uvicorn applies its ``LOGGING_CONFIG`` through
    :func:`logging.config.dictConfig` at startup, which **replaces** a configured
    logger's handlers (a handler-level filter would be thrown away with them) but
    does not clear filters attached programmatically to the logger. If a future
    uvicorn/stdlib release changed that, this test fails and the leak would
    otherwise return silently.
    """
    import uvicorn.config

    install_access_log_redaction()
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)

    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, AccessLogRedactionFilter) for f in access.filters)

    # Still functional, not merely present: push a record through the logger's own
    # filter chain the way `Logger.handle` does. (`Filterer.filter` is truthy on
    # "keep" — since 3.12 it may return the record itself rather than `True`.)
    record = _access_record("/api/health?token=SENTINEL")
    assert access.filter(record)
    assert "SENTINEL" not in record.getMessage()
    assert "redacted" in record.getMessage()
