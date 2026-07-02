"""trading_bot transport layer — async I/O primitives.

Unlike :mod:`trading_bot.domain` (pure, synchronous, no I/O), this layer **does**
I/O (httpx). It stays venue-neutral plumbing: no auth/signing (that is the
broker layer) and no domain business logic.

Public surface:

* :class:`~trading_bot.transport.http.AsyncHTTPClient` — async httpx wrapper with
  retry/backoff, timeouts, and ``get`` / ``post`` (the latter with an
  opt-out-of-retries ``retry`` flag for non-idempotent POSTs);
* :class:`~trading_bot.transport.http.HTTPError` — transport-local HTTP failure.
* :class:`~trading_bot.transport.http.AmbiguousRequestError` — a non-retryable
  request failed ambiguously (reconcile before retrying).
* :class:`~trading_bot.transport.http.ResponseTooLargeError` — a response body
  exceeded the transport's size cap and was refused before buffering/parsing.
* :class:`~trading_bot.transport.ws.WebSocketBase` — async WebSocket base with
  ``stream_raw`` and exponential reconnect.
* :class:`~trading_bot.transport.ratelimit.RateLimiter` /
  :class:`~trading_bot.transport.ratelimit.TokenBucket` — proactive
  per-exchange token-bucket throttling.
* :class:`~trading_bot.transport.ratelimit.WeightBucket` — Binance's rolling
  request-*weight* budget (weight-aware throttling + 418/429 back-off).
* :class:`~trading_bot.transport.ratelimit.KrakenCallCounter` — Kraken's
  decaying private-endpoint call counter.
"""

from __future__ import annotations

from trading_bot.transport.http import (
    AmbiguousRequestError,
    AsyncHTTPClient,
    HTTPError,
    ResponseTooLargeError,
)
from trading_bot.transport.ratelimit import (
    KrakenCallCounter,
    RateLimiter,
    TokenBucket,
    WeightBucket,
)
from trading_bot.transport.ws import WebSocketBase

__all__ = [
    "AmbiguousRequestError",
    "AsyncHTTPClient",
    "HTTPError",
    "KrakenCallCounter",
    "RateLimiter",
    "ResponseTooLargeError",
    "TokenBucket",
    "WebSocketBase",
    "WeightBucket",
]
