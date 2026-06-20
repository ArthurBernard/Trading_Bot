"""trading_bot transport layer — async I/O primitives.

Unlike :mod:`trading_bot.domain` (pure, synchronous, no I/O), this layer **does**
I/O (httpx). It stays venue-neutral plumbing: no auth/signing (that is the
broker layer) and no domain business logic.

Public surface:

* :class:`~trading_bot.transport.http.AsyncHTTPClient` — async httpx wrapper with
  retry/backoff, timeouts, and ``get`` / ``post``;
* :class:`~trading_bot.transport.http.HTTPError` — transport-local HTTP failure.
* :class:`~trading_bot.transport.ws.WebSocketBase` — async WebSocket base with
  ``stream_raw`` and exponential reconnect.
"""

from __future__ import annotations

from trading_bot.transport.http import AsyncHTTPClient, HTTPError
from trading_bot.transport.ws import WebSocketBase

__all__ = ["AsyncHTTPClient", "HTTPError", "WebSocketBase"]
