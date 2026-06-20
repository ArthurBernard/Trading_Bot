"""Tests for :mod:`trading_bot.transport.ws`.

Offline tests drive :class:`WebSocketBase` through injected ``connect`` and
``sleep`` seams: a fake connection can raise on connect (to exercise reconnect)
then yield a scripted list of frames, while a recording sleep asserts the
backoff schedule without real waits. The opt-in network test (``-m network``)
hits Kraken's public v2 WebSocket.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from trading_bot.transport import WebSocketBase


class RecordingSleep:
    """Async ``asyncio.sleep`` stand-in that records every requested delay."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


class FakeWS:
    """A fake live connection: async-iterates *frames* and records sends."""

    def __init__(self, frames: list[str | bytes]) -> None:
        self._frames = frames
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        async def _gen() -> AsyncIterator[str | bytes]:
            for frame in self._frames:
                yield frame

        return _gen()


class _Conn:
    """Async context manager standing in for ``websockets.connect(url)``.

    ``__aenter__`` either raises a scripted exception (failed connect) or
    returns the scripted :class:`FakeWS` (successful connect).
    """

    def __init__(self, result: BaseException | FakeWS) -> None:
        self._result = result

    async def __aenter__(self) -> FakeWS:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeConnector:
    """Injectable ``connect`` seam yielding a scripted sequence of attempts.

    Each call pops the next scripted *result*: a :class:`BaseException` (the
    connect fails) or a :class:`FakeWS` (the connect succeeds and yields its
    frames). Records the per-call URL for assertions.
    """

    def __init__(self, results: list[BaseException | FakeWS]) -> None:
        self._results = list(results)
        self.urls: list[str] = []

    def __call__(self, url: str) -> _Conn:
        self.urls.append(url)
        if not self._results:
            raise AssertionError("FakeConnector called more times than scripted")
        return _Conn(self._results.pop(0))


async def test_reconnects_after_failure_and_yields_all_frames() -> None:
    # First connect raises, second succeeds and yields two frames.
    ws = FakeWS(["frame-1", "frame-2"])
    connector = FakeConnector([ConnectionError("boom"), ws])
    sleep = RecordingSleep()
    base = WebSocketBase(
        "wss://example.test", connect=connector, sleep=sleep, backoff_base=0.5
    )

    received: list[str | bytes] = []
    async for frame in base.stream_raw():
        received.append(frame)
        if len(received) == 2:
            base.stop()

    assert received == ["frame-1", "frame-2"]
    # Exactly one failed connect → exactly one backoff sleep, at base * 2**0.
    assert sleep.calls == [pytest.approx(0.5)]


async def test_backoff_increasing_capped_and_resets_after_success() -> None:
    # Three consecutive failed connects, then a success that yields one frame.
    ws = FakeWS(["ok"])
    connector = FakeConnector(
        [
            ConnectionError("1"),
            ConnectionError("2"),
            ConnectionError("3"),
            ws,
        ]
    )
    sleep = RecordingSleep()
    base = WebSocketBase(
        "wss://example.test",
        connect=connector,
        sleep=sleep,
        backoff_base=0.5,
        max_backoff=1.0,  # low cap so the 3rd failure is clamped
    )

    received: list[str | bytes] = []
    async for frame in base.stream_raw():
        received.append(frame)
        base.stop()

    assert received == ["ok"]
    # Increasing then capped: 0.5*2**0, 0.5*2**1, then clamped to max_backoff=1.0.
    assert sleep.calls == [
        pytest.approx(0.5),
        pytest.approx(1.0),
        pytest.approx(1.0),
    ]


async def test_backoff_resets_to_base_after_a_good_connect() -> None:
    # success (1 frame) → fail → success (1 frame). The failure after a good
    # connect must use base*2**0, proving the attempt counter reset.
    ws1 = FakeWS(["a"])
    ws2 = FakeWS(["b"])
    connector = FakeConnector([ws1, ConnectionError("blip"), ws2])
    sleep = RecordingSleep()
    base = WebSocketBase(
        "wss://example.test", connect=connector, sleep=sleep, backoff_base=0.5
    )

    received: list[str | bytes] = []
    async for frame in base.stream_raw():
        received.append(frame)
        if len(received) == 2:
            base.stop()

    assert received == ["a", "b"]
    # The single reconnect waited base*2**0 (not a larger inherited delay).
    assert sleep.calls == [pytest.approx(0.5)]


async def test_on_connect_called_on_every_reconnect() -> None:
    calls: list[int] = []

    class Counting(WebSocketBase):
        async def on_connect(self, ws: Any) -> None:
            calls.append(1)

    ws1 = FakeWS(["a"])
    ws2 = FakeWS(["b"])
    # fail, success, fail, success → two successful connects → two on_connect.
    connector = FakeConnector(
        [ConnectionError("x"), ws1, ConnectionError("y"), ws2]
    )
    sleep = RecordingSleep()
    base = Counting("wss://example.test", connect=connector, sleep=sleep)

    received: list[str | bytes] = []
    async for frame in base.stream_raw():
        received.append(frame)
        if len(received) == 2:
            base.stop()

    assert received == ["a", "b"]
    # on_connect fires once per *successful* connect, not on failed attempts.
    assert len(calls) == 2


async def test_send_writes_to_active_socket_via_on_connect() -> None:
    ws = FakeWS(["frame"])

    class Subscriber(WebSocketBase):
        async def on_connect(self, sock: Any) -> None:
            await self.send({"method": "subscribe"})

    connector = FakeConnector([ws])
    base = Subscriber("wss://example.test", connect=connector)

    async for _ in base.stream_raw():
        base.stop()

    assert ws.sent == [{"method": "subscribe"}]


async def test_send_while_disconnected_raises() -> None:
    base = WebSocketBase("wss://example.test", connect=FakeConnector([]))
    with pytest.raises(RuntimeError):
        await base.send("nope")


async def test_stop_before_start_yields_nothing() -> None:
    connector = FakeConnector([FakeWS(["never"])])
    base = WebSocketBase("wss://example.test", connect=connector)
    base.stop()

    received = [frame async for frame in base.stream_raw()]
    assert received == []


@pytest.mark.network
async def test_real_kraken_ticker_frame() -> None:
    """Live subscribe to Kraken v2 ``ticker`` for BTC/USD (opt-in: ``-m network``).

    Subscribes from :meth:`on_connect`, asserts at least one real frame arrives,
    then stops. The first frame is typically the subscription ack.
    """

    class KrakenTicker(WebSocketBase):
        async def on_connect(self, ws: Any) -> None:
            await self.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "params": {"channel": "ticker", "symbol": ["BTC/USD"]},
                    }
                )
            )

    base = KrakenTicker("wss://ws.kraken.com/v2")
    frames: list[str | bytes] = []
    async for frame in base.stream_raw():
        frames.append(frame)
        base.stop()

    assert frames
    payload = json.loads(frames[0])
    assert isinstance(payload, dict)
