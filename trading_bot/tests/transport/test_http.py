"""Tests for :mod:`trading_bot.transport.http`.

Offline tests use ``pytest-httpx``'s ``httpx_mock`` fixture and a recording
fake sleep injected via the ``sleep`` seam, so retry timing is asserted without
real waits. The opt-in network test (``-m network``) hits Kraken's public API.
"""

from __future__ import annotations

import httpx
import pytest

from trading_bot.transport import (
    AmbiguousRequestError,
    AsyncHTTPClient,
    HTTPError,
)


class RecordingSleep:
    """Async ``asyncio.sleep`` stand-in that records every requested delay."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


async def test_retries_503_then_returns_200_json(httpx_mock) -> None:
    httpx_mock.add_response(status_code=503)
    httpx_mock.add_response(status_code=200, json={"ok": True})
    sleep = RecordingSleep()

    async with AsyncHTTPClient(backoff_base=0.5, sleep=sleep) as client:
        result = await client.get("https://example.test/data")

    assert result == {"ok": True}
    # One transient 503 → exactly one backoff sleep, at backoff_base * 2**0 == 0.5.
    assert sleep.calls == [pytest.approx(0.5)]


async def test_non_retryable_400_raises_http_error(httpx_mock) -> None:
    httpx_mock.add_response(status_code=400, text="bad request body")
    sleep = RecordingSleep()

    async with AsyncHTTPClient(sleep=sleep) as client:
        with pytest.raises(HTTPError) as exc_info:
            await client.get("https://example.test/oops")

    err = exc_info.value
    assert err.status == 400
    assert err.url == "https://example.test/oops"
    assert err.body == "bad request body"
    # 4xx is not retried: the sleep seam was never awaited.
    assert sleep.calls == []


async def test_post_sends_body_and_parses_json(httpx_mock) -> None:
    httpx_mock.add_response(status_code=200, json={"txid": ["OABC-123"]})
    sleep = RecordingSleep()

    async with AsyncHTTPClient(sleep=sleep) as client:
        result = await client.post(
            "https://example.test/AddOrder",
            data={"pair": "XBTUSD", "type": "buy"},
        )

    assert result == {"txid": ["OABC-123"]}
    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "POST"
    # Form body is sent and url-encoded.
    body = request.content.decode()
    assert "pair=XBTUSD" in body
    assert "type=buy" in body


async def test_post_sends_json_body(httpx_mock) -> None:
    httpx_mock.add_response(status_code=200, json={"accepted": True})

    async with AsyncHTTPClient() as client:
        result = await client.post(
            "https://example.test/json", json={"a": 1, "b": [2, 3]}
        )

    assert result == {"accepted": True}
    request = httpx_mock.get_request()
    assert request is not None
    import json as _json

    assert _json.loads(request.content) == {"a": 1, "b": [2, 3]}


async def test_429_retry_after_waits_then_retries(httpx_mock) -> None:
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "3"})
    httpx_mock.add_response(status_code=200, json={"ok": 1})
    sleep = RecordingSleep()

    async with AsyncHTTPClient(sleep=sleep) as client:
        result = await client.get("https://example.test/limited")

    assert result == {"ok": 1}
    # Retry-After honoured exactly via the seam.
    assert sleep.calls == [pytest.approx(3.0)]


async def test_max_retries_exhausted_on_persistent_503(httpx_mock) -> None:
    for _ in range(3):
        httpx_mock.add_response(status_code=503, text="upstream down")
    sleep = RecordingSleep()

    async with AsyncHTTPClient(max_retries=3, sleep=sleep) as client:
        with pytest.raises(HTTPError) as exc_info:
            await client.get("https://example.test/down")

    assert exc_info.value.status == 503
    # Three attempts → three *increasing* backoff sleeps (0.5*2**0, *2**1, *2**2).
    assert sleep.calls == [
        pytest.approx(0.5),
        pytest.approx(1.0),
        pytest.approx(2.0),
    ]


async def test_retries_on_transport_error(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    httpx_mock.add_response(status_code=200, json={"recovered": True})
    sleep = RecordingSleep()

    async with AsyncHTTPClient(sleep=sleep) as client:
        result = await client.get("https://example.test/flaky")

    assert result == {"recovered": True}
    assert sleep.calls == [pytest.approx(0.5)]


# --- non-idempotent POST: retry=False (venue-idempotency guard) ------------ #


async def test_post_no_retry_succeeds_at_most_once(httpx_mock) -> None:
    """A ``retry=False`` POST that succeeds is sent exactly once, returns JSON."""
    httpx_mock.add_response(status_code=200, json={"txid": ["OABC-1"]})
    sleep = RecordingSleep()

    async with AsyncHTTPClient(sleep=sleep) as client:
        result = await client.post(
            "https://example.test/AddOrder",
            data={"pair": "XBTUSD"},
            retry=False,
        )

    assert result == {"txid": ["OABC-1"]}
    # Exactly one request was made; no backoff sleep.
    assert len(httpx_mock.get_requests()) == 1
    assert sleep.calls == []


async def test_post_no_retry_5xx_raises_ambiguous_not_retried(httpx_mock) -> None:
    """A 5xx on a ``retry=False`` POST → ``AmbiguousRequestError``, sent once.

    The order may have landed at the venue before the 5xx; the client must NOT
    retry (that could double-submit) and must signal the caller to reconcile.
    """
    httpx_mock.add_response(status_code=503, text="upstream down")
    sleep = RecordingSleep()

    async with AsyncHTTPClient(max_retries=3, sleep=sleep) as client:
        with pytest.raises(AmbiguousRequestError) as exc_info:
            await client.post(
                "https://example.test/AddOrder",
                data={"pair": "XBTUSD"},
                retry=False,
            )

    # Exactly one attempt (no retry), no backoff sleep.
    assert len(httpx_mock.get_requests()) == 1
    assert sleep.calls == []
    # The error tells the caller to reconcile, never blind-retry.
    msg = str(exc_info.value)
    assert "reconcile" in msg
    assert "503" in msg


async def test_post_no_retry_transport_error_raises_ambiguous(httpx_mock) -> None:
    """A transport error on a ``retry=False`` POST → ``AmbiguousRequestError``.

    A dropped connection after the request left is the canonical ambiguous case.
    """
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    sleep = RecordingSleep()

    async with AsyncHTTPClient(max_retries=3, sleep=sleep) as client:
        with pytest.raises(AmbiguousRequestError, match="reconcile"):
            await client.post(
                "https://example.test/AddOrder",
                data={"pair": "XBTUSD"},
                retry=False,
            )

    assert sleep.calls == []


async def test_post_no_retry_429_raises_ambiguous(httpx_mock) -> None:
    """A 429 on a ``retry=False`` POST → ``AmbiguousRequestError`` (not retried)."""
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "3"})
    sleep = RecordingSleep()

    async with AsyncHTTPClient(sleep=sleep) as client:
        with pytest.raises(AmbiguousRequestError, match="reconcile"):
            await client.post(
                "https://example.test/AddOrder",
                data={"pair": "XBTUSD"},
                retry=False,
            )

    assert sleep.calls == []


async def test_post_no_retry_4xx_raises_http_error_not_ambiguous(httpx_mock) -> None:
    """A definite 4xx on a ``retry=False`` POST raises ``HTTPError`` (not ambiguous).

    A 4xx rejection definitely did NOT take effect, so there is nothing to
    reconcile: it is the ordinary non-retryable :class:`HTTPError`, same as the
    retrying path.
    """
    httpx_mock.add_response(status_code=400, text="bad request")

    async with AsyncHTTPClient() as client:
        with pytest.raises(HTTPError) as exc_info:
            await client.post(
                "https://example.test/AddOrder",
                data={"pair": "XBTUSD"},
                retry=False,
            )

    assert exc_info.value.status == 400
    assert not isinstance(exc_info.value, AmbiguousRequestError)


async def test_post_default_retries_5xx_then_succeeds(httpx_mock) -> None:
    """The default (``retry=True``) POST path is unchanged: 5xx is retried.

    Proves the opt-out is opt-*in*: an idempotent POST still gets bounded retry.
    """
    httpx_mock.add_response(status_code=503)
    httpx_mock.add_response(status_code=200, json={"ok": True})
    sleep = RecordingSleep()

    async with AsyncHTTPClient(backoff_base=0.5, sleep=sleep) as client:
        result = await client.post(
            "https://example.test/Balance", data={"nonce": "1"}
        )

    assert result == {"ok": True}
    assert sleep.calls == [pytest.approx(0.5)]


async def test_get_path_unchanged_by_retry_flag(httpx_mock) -> None:
    """GET keeps retrying 5xx (the idempotent read path is never opted out)."""
    httpx_mock.add_response(status_code=503)
    httpx_mock.add_response(status_code=200, json={"ok": 1})
    sleep = RecordingSleep()

    async with AsyncHTTPClient(backoff_base=0.5, sleep=sleep) as client:
        result = await client.get("https://example.test/data")

    assert result == {"ok": 1}
    assert sleep.calls == [pytest.approx(0.5)]


async def test_requires_context_manager() -> None:
    client = AsyncHTTPClient()
    with pytest.raises(RuntimeError):
        await client.get("https://example.test/nope")


async def test_limiter_acquired_before_request(httpx_mock) -> None:
    httpx_mock.add_response(status_code=200, json={"ok": True})

    class FakeLimiter:
        def __init__(self) -> None:
            self.acquired: list[str | None] = []

        async def acquire(self, exchange: str | None) -> None:
            self.acquired.append(exchange)

    limiter = FakeLimiter()
    async with AsyncHTTPClient(exchange="kraken", limiter=limiter) as client:
        await client.get("https://example.test/data")

    assert limiter.acquired == ["kraken"]


@pytest.mark.network
async def test_real_kraken_time() -> None:
    """Live GET against Kraken's public Time endpoint (opt-in: ``-m network``)."""
    async with AsyncHTTPClient() as client:
        payload = await client.get("https://api.kraken.com/0/public/Time")

    assert isinstance(payload, dict)
    unixtime = payload["result"]["unixtime"]
    assert isinstance(unixtime, int)
    assert unixtime > 0
