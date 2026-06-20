"""trading_bot transport layer — async I/O primitives.

Unlike :mod:`trading_bot.domain` (pure, synchronous, no I/O), this layer **does**
I/O (httpx). It stays venue-neutral plumbing: no auth/signing (that is the
broker layer) and no domain business logic.

Public surface:

* :class:`~trading_bot.transport.http.AsyncHTTPClient` — async httpx wrapper with
  retry/backoff, timeouts, and ``get`` / ``post``;
* :class:`~trading_bot.transport.http.HTTPError` — transport-local HTTP failure.
"""

from __future__ import annotations

from trading_bot.transport.http import AsyncHTTPClient, HTTPError

__all__ = ["AsyncHTTPClient", "HTTPError"]
