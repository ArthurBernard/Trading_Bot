"""Async WebSocket base with auto-reconnect.

Thin async wrapper over the ``websockets`` library for the execution layer. It
provides a raw-frame stream (:meth:`WebSocketBase.stream_raw`) that reconnects
on disconnect/error with **monotonically increasing, capped** exponential
backoff, plus an overridable :meth:`WebSocketBase.on_connect` hook and a
:meth:`WebSocketBase.send` helper.

It is **venue-neutral plumbing**: it does no auth/signing and sends no
subscription payloads of its own (that belongs to the broker layer, which
subclasses this base and overrides :meth:`WebSocketBase.on_connect`). It does
not import domain business logic.

Mirrors ``dccd.transport.ws.WebSocketBase``; adapted for execution by injecting
the ``connect`` and ``sleep`` callables as seams so reconnect timing and
behaviour are testable without a real server (matching the ``sleep`` seam and
``_MAX_BACKOFF`` cap of the sibling :mod:`trading_bot.transport.http`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

__all__ = ["WebSocketBase"]

logger = logging.getLogger(__name__)

_DEFAULT_BACKOFF_BASE = 0.5
# Cap a single reconnect backoff so a sustained outage never parks the stream
# for an unreasonable time. Mirrors ``http._MAX_BACKOFF``.
_DEFAULT_MAX_BACKOFF = 60.0


class _WSConnection(Protocol):
    """Minimal structural type for a live ``websockets`` connection.

    Typed as a :class:`~typing.Protocol` so the ``connect`` seam can yield any
    object exposing ``send`` and async iteration — the real
    ``websockets.ClientConnection`` and a test fake both satisfy it.
    """

    async def send(self, message: Any) -> None:
        """Send one frame on the connection."""
        ...

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        """Iterate inbound frames."""
        ...


class _Connector(Protocol):
    """Structural type for the injected ``connect`` callable.

    Must return an async context manager that yields a :class:`_WSConnection`
    on ``__aenter__`` (exactly how ``websockets.connect(url)`` behaves). A test
    fake can therefore raise on ``__aenter__`` to simulate a failed connect,
    then yield a frame-producing fake on a later attempt.
    """

    def __call__(self, url: str) -> Any:
        """Open a connection to *url*; returns an async context manager."""
        ...


def _default_connect(url: str) -> Any:
    """Open a real ``websockets`` connection (imported lazily).

    ``close_timeout=1`` keeps shutdown snappy: without it the closing handshake
    can block several seconds on :meth:`WebSocketBase.stop`/cancel.
    """
    import websockets

    return websockets.connect(url, close_timeout=1)


class WebSocketBase:
    """Base async WebSocket client with exponential reconnect.

    Subclasses override :meth:`on_connect` to send subscription/auth messages
    after each (re)connect, and consume :meth:`stream_raw` (or :meth:`stream`
    via an overridden :meth:`parse_message`) for inbound frames.

    Parameters
    ----------
    url : str
        WebSocket endpoint URL.
    max_backoff : float, default 60.0
        Upper bound (seconds) on a single reconnect backoff sleep.
    backoff_base : float, default 0.5
        Exponential backoff base, in seconds: the zero-based reconnect
        *attempt* ``n`` waits ``backoff_base * 2**n`` (0.5, 1.0, 2.0, … —
        increasing, capped at *max_backoff*). The attempt counter resets to 0
        after every successful connect, so a brief blip never inherits a long
        delay from an earlier outage.
    connect : callable, optional
        ``websockets.connect``-compatible factory returning an async context
        manager that yields the live connection. Injected as a seam so
        reconnect behaviour is testable without a real server.
    sleep : callable, optional
        ``asyncio.sleep``-compatible coroutine used for backoff waits. Injected
        as a seam so reconnect timing is testable without real waits.

    Notes
    -----
    The active connection is exposed (privately) only while
    :meth:`stream_raw` holds it, so :meth:`send` can write subscription/auth
    frames mid-stream. Calling :meth:`send` while disconnected raises
    :class:`RuntimeError` rather than silently dropping the frame — the caller
    should subscribe from :meth:`on_connect` (re-run on every reconnect) and
    treat a live socket as a precondition for ad-hoc sends.
    """

    def __init__(
        self,
        url: str,
        *,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        connect: _Connector = _default_connect,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.url = url
        self._max_backoff = max_backoff
        self._backoff_base = backoff_base
        self._connect = connect
        self._sleep = sleep
        self._stop = asyncio.Event()
        self._ws: _WSConnection | None = None

    def stop(self) -> None:
        """Request graceful shutdown of the stream."""
        self._stop.set()

    def _backoff(self, attempt: int) -> float:
        """Backoff delay (seconds) for a zero-based *attempt*, capped.

        Monotonically increasing exponential backoff: ``backoff_base * 2**attempt``
        (a sustained outage waits longer each retry, never shorter), capped at
        ``max_backoff``.
        """
        return min(self._backoff_base * 2.0**attempt, self._max_backoff)

    async def on_connect(self, ws: _WSConnection) -> None:
        """Called after each (re)connect. Override to send subscriptions/auth.

        Default is a no-op (this base is venue-neutral). Because it runs on
        every reconnect, subscriptions are automatically re-established.
        """

    async def send(self, message: Any) -> None:
        """Send a frame on the live connection.

        Parameters
        ----------
        message : Any
            Payload to send (``str`` / ``bytes``, as ``websockets`` accepts).

        Raises
        ------
        RuntimeError
            If called while no connection is active (i.e. outside an active
            :meth:`stream_raw`, or during a reconnect gap). Re-establish
            subscriptions from :meth:`on_connect` instead of relying on sends
            surviving a disconnect.
        """
        ws = self._ws
        if ws is None:
            raise RuntimeError("send() called while WebSocket is not connected")
        await ws.send(message)

    async def parse_message(self, raw: str | bytes) -> AsyncIterator[Any]:
        """Parse a raw frame and yield records. Override in subclass.

        Default yields nothing; brokers override this to turn frames into
        domain-facing records (the base stays venue-neutral).
        """
        return
        yield  # pragma: no cover - makes this an async generator

    async def stream(self) -> AsyncIterator[Any]:
        """Yield parsed records, reconnecting on errors.

        Delegates frame parsing to :meth:`parse_message`. Kept thin to mirror
        dccd; most execution adapters consume :meth:`stream_raw` directly.
        """
        async for raw in self.stream_raw():
            async for record in self.parse_message(raw):
                yield record

    async def stream_raw(self) -> AsyncIterator[str | bytes]:
        """Yield raw WebSocket frames, reconnecting with exponential backoff.

        Connects, runs :meth:`on_connect`, then yields inbound frames. On any
        disconnect or error it reconnects with **increasing, capped**
        exponential backoff (the attempt counter resets after each successful
        connect). The loop ends cleanly once :meth:`stop` is called.

        Yields
        ------
        str or bytes
            Raw inbound WebSocket frames, in arrival order.
        """
        attempt = 0
        while not self._stop.is_set():
            try:
                async with self._connect(self.url) as ws:
                    # Successful connect: reset backoff and expose the socket
                    # so send() can write while the stream runs.
                    attempt = 0
                    self._ws = ws
                    await self.on_connect(ws)
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        yield raw
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._stop.is_set():
                    return
                delay = self._backoff(attempt)
                logger.warning(
                    "WS %s disconnected: %s — reconnect in %.1fs",
                    self.url,
                    exc,
                    delay,
                )
                await self._sleep(delay)
                attempt += 1
            finally:
                self._ws = None
