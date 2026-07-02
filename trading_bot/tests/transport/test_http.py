"""Tests for :mod:`trading_bot.transport.http`.

Offline tests use ``pytest-httpx``'s ``httpx_mock`` fixture and a recording
fake sleep injected via the ``sleep`` seam, so retry timing is asserted without
real waits. The opt-in network test (``-m network``) hits Kraken's public API.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from trading_bot.transport import (
    AmbiguousRequestError,
    AsyncHTTPClient,
    HTTPError,
    ResponseTooLargeError,
)
from trading_bot.transport.http import (
    _MAX_CONNECTIONS,
    _MAX_KEEPALIVE_CONNECTIONS,
    _MAX_RESPONSE_BYTES,
    _redact_url,
)

# --- Fake secrets --------------------------------------------------------- #
# Synthetic, NOT real credentials. A 64-hex-char stand-in for a Binance
# HMAC-SHA256 signature and a fake API key. Both must never survive into a log
# record or an exception message.
_FAKE_SIGNATURE = "deadbeef" * 8  # 64 hex chars, shaped like a real HMAC
_FAKE_API_KEY = "FAKEKEY0000000000000000000000000000000000000000000000000000000000"
# A realistic *signed* Binance-style URL: caller params, then apiKey / timestamp,
# then the appended signature — exactly the shape ``binance._signed_request``
# hands to the transport.
_SIGNED_URL = (
    "https://api.binance.test/api/v3/order"
    "?symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=0.01&price=50000"
    f"&recvWindow=5000&timestamp=1700000000000&apiKey={_FAKE_API_KEY}"
    f"&signature={_FAKE_SIGNATURE}"
)


def _assert_no_secret(text: str) -> None:
    """Assert *text* leaks neither the fake signature nor the fake API key."""
    assert _FAKE_SIGNATURE not in text
    assert _FAKE_API_KEY not in text


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
            self.acquired: list[tuple[str | None, float]] = []

        async def acquire(
            self, exchange: str | None, weight: float = 1.0
        ) -> None:
            self.acquired.append((exchange, weight))

    limiter = FakeLimiter()
    async with AsyncHTTPClient(exchange="kraken", limiter=limiter) as client:
        await client.get("https://example.test/data", weight=3.0)

    # The exchange and the per-call weight are both forwarded to the limiter.
    assert limiter.acquired == [("kraken", 3.0)]


# --- weight-aware limiter feedback (observe / penalise) ------------------- #


class _FeedbackLimiter:
    """A weight-aware limiter recording every acquire / observe / penalise.

    Exposes the optional ``observe`` / ``penalise`` hooks so the HTTP client's
    feedback path (used-weight resync + 418/429 back-off) is asserted directly
    against a fake — no real limiter, no network.
    """

    def __init__(self) -> None:
        self.acquired: list[tuple[str | None, float]] = []
        self.observed: list[tuple[str | None, float]] = []
        self.penalised: list[tuple[str | None, float]] = []

    async def acquire(self, exchange: str | None, weight: float = 1.0) -> None:
        self.acquired.append((exchange, weight))

    def observe(self, exchange: str | None, used_weight: float) -> None:
        self.observed.append((exchange, used_weight))

    def penalise(self, exchange: str | None, seconds: float) -> None:
        self.penalised.append((exchange, seconds))


async def test_used_weight_header_fed_back_to_limiter(httpx_mock) -> None:
    """A ``X-MBX-USED-WEIGHT-1M`` response header resyncs the limiter."""
    httpx_mock.add_response(
        status_code=200, json={"ok": True}, headers={"X-MBX-USED-WEIGHT-1M": "137"}
    )
    limiter = _FeedbackLimiter()

    async with AsyncHTTPClient(exchange="binance", limiter=limiter) as client:
        await client.get("https://example.test/data", weight=2.0)

    assert limiter.acquired == [("binance", 2.0)]
    # The venue-reported used weight is fed back for the next call to throttle on.
    assert limiter.observed == [("binance", 137.0)]


async def test_429_retry_after_penalises_limiter_then_retries(httpx_mock) -> None:
    """A retried 429 feeds ``Retry-After`` to the limiter AND waits it out."""
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "4"})
    httpx_mock.add_response(status_code=200, json={"ok": 1})
    sleep = RecordingSleep()
    limiter = _FeedbackLimiter()

    async with AsyncHTTPClient(
        exchange="binance", limiter=limiter, sleep=sleep
    ) as client:
        result = await client.get("https://example.test/limited")

    assert result == {"ok": 1}
    # The next call is told to wait ~4s (penalise), and this call slept ~4s too.
    assert limiter.penalised == [("binance", pytest.approx(4.0))]
    assert sleep.calls == [pytest.approx(4.0)]


async def test_418_ip_ban_penalises_and_backs_off(httpx_mock) -> None:
    """A 418 (IP auto-ban) honours ``Retry-After``: penalise + wait, then retry."""
    httpx_mock.add_response(status_code=418, headers={"Retry-After": "120"})
    httpx_mock.add_response(status_code=200, json={"ok": True})
    sleep = RecordingSleep()
    limiter = _FeedbackLimiter()

    async with AsyncHTTPClient(
        exchange="binance", limiter=limiter, sleep=sleep
    ) as client:
        result = await client.get("https://example.test/banned")

    assert result == {"ok": True}
    # 418 is treated as a rate-limit/ban class: back off the full Retry-After,
    # capped at _MAX_BACKOFF (60s) by _retry_after.
    assert limiter.penalised == [("binance", pytest.approx(60.0))]
    assert sleep.calls == [pytest.approx(60.0)]


async def test_no_retry_429_still_penalises_but_never_retries(httpx_mock) -> None:
    """A ``retry=False`` 429 raises ambiguous AND surfaces Retry-After to the limiter.

    The order submit must NOT auto-retry (ambiguous → reconcile), yet the
    advised back-off is still fed to the limiter so the *next* (post-reconcile)
    call waits the venue's window — B-9.
    """
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "9"})
    sleep = RecordingSleep()
    limiter = _FeedbackLimiter()

    async with AsyncHTTPClient(
        exchange="binance", limiter=limiter, sleep=sleep
    ) as client:
        with pytest.raises(AmbiguousRequestError, match="reconcile"):
            await client.post(
                "https://example.test/order",
                data={"symbol": "BTCUSDT"},
                retry=False,
            )

    # Sent at most once: no blind-retry, no per-call backoff sleep on this path.
    assert len(httpx_mock.get_requests()) == 1
    assert sleep.calls == []
    # But the Retry-After was surfaced to the limiter for the NEXT call.
    assert limiter.penalised == [("binance", pytest.approx(9.0))]


# --- secret redaction: the helper ----------------------------------------- #


def test_redact_url_no_query_unchanged() -> None:
    """A URL with no query string is returned verbatim."""
    url = "https://api.binance.test/api/v3/order"
    assert _redact_url(url) == url


def test_redact_url_no_sensitive_params_unchanged() -> None:
    """A query with only innocuous params is preserved (diagnostics stay useful)."""
    url = "https://api.binance.test/api/v3/order?symbol=BTCUSDT&side=BUY"
    assert _redact_url(url) == url


def test_redact_url_masks_signature_at_end() -> None:
    """The trailing ``&signature=`` value is masked; others survive."""
    url = f"https://x.test/o?symbol=BTCUSDT&signature={_FAKE_SIGNATURE}"
    out = _redact_url(url)
    assert "signature=%3Credacted%3E" in out or "signature=<redacted>" in out
    assert _FAKE_SIGNATURE not in out
    assert "symbol=BTCUSDT" in out


def test_redact_url_masks_param_at_start() -> None:
    """A sensitive param first in the query is masked."""
    url = f"https://x.test/o?token={_FAKE_SIGNATURE}&symbol=BTCUSDT"
    out = _redact_url(url)
    assert _FAKE_SIGNATURE not in out
    assert "symbol=BTCUSDT" in out


def test_redact_url_masks_param_in_middle() -> None:
    """A sensitive param sandwiched between innocuous ones is masked."""
    url = (
        "https://x.test/o?symbol=BTCUSDT"
        f"&apiKey={_FAKE_API_KEY}&recvWindow=5000"
    )
    out = _redact_url(url)
    assert _FAKE_API_KEY not in out
    assert "symbol=BTCUSDT" in out
    assert "recvWindow=5000" in out


def test_redact_url_masks_all_sensitive_keys() -> None:
    """Every configured sensitive key (any case) has its value masked."""
    url = (
        "https://x.test/o?"
        "signature=AAA&api_key=BBB&apiKey=CCC&token=DDD&nonce=EEE&symbol=BTC"
    )
    out = _redact_url(url)
    for secret in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        assert f"={secret}" not in out
    assert "symbol=BTC" in out
    assert out.count("redacted") == 5


def test_redact_url_multiple_params_full_signed_url() -> None:
    """A full signed Binance-style URL loses only its secrets."""
    out = _redact_url(_SIGNED_URL)
    _assert_no_secret(out)
    # Non-sensitive params are preserved for diagnostics.
    assert "symbol=BTCUSDT" in out
    assert "side=BUY" in out
    assert "redacted" in out


# --- secret redaction: exception constructors ----------------------------- #


def test_http_error_redacts_url_in_message_and_attr() -> None:
    """``HTTPError`` stores and renders only the redacted URL."""
    err = HTTPError(500, _SIGNED_URL, body="upstream down")
    _assert_no_secret(str(err))
    _assert_no_secret(err.url)
    # ``args`` are what a downstream ``logger.error("%s", err)`` would render.
    _assert_no_secret("".join(str(a) for a in err.args))
    assert "redacted" in str(err)


def test_ambiguous_error_redacts_url_in_message_and_attr() -> None:
    """``AmbiguousRequestError`` stores and renders only the redacted URL."""
    err = AmbiguousRequestError(_SIGNED_URL, reason="HTTP 503")
    _assert_no_secret(str(err))
    _assert_no_secret(err.url)
    _assert_no_secret("".join(str(a) for a in err.args))
    assert "reconcile" in str(err)


# --- secret redaction: end-to-end error + logging paths ------------------- #


async def test_signed_url_5xx_retries_exhausted_no_secret_leak(
    httpx_mock, caplog
) -> None:
    """A persistent 5xx on a signed URL leaks no secret in logs or the error."""
    for _ in range(3):
        httpx_mock.add_response(status_code=503, text="upstream down")
    sleep = RecordingSleep()

    with caplog.at_level(logging.DEBUG, logger="trading_bot.transport.http"):
        async with AsyncHTTPClient(max_retries=3, sleep=sleep) as client:
            with pytest.raises(HTTPError) as exc_info:
                await client.get(_SIGNED_URL)

    _assert_no_secret(str(exc_info.value))
    _assert_no_secret(caplog.text)
    # Prove the redaction fired rather than the URL simply being absent.
    assert "redacted" in caplog.text
    assert "redacted" in str(exc_info.value)


async def test_signed_url_429_no_secret_leak(httpx_mock, caplog) -> None:
    """A 429 (rate-limited) log on a signed URL carries no secret."""
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "1"})
    httpx_mock.add_response(status_code=200, json={"ok": True})
    sleep = RecordingSleep()

    with caplog.at_level(logging.WARNING, logger="trading_bot.transport.http"):
        async with AsyncHTTPClient(sleep=sleep) as client:
            result = await client.get(_SIGNED_URL)

    assert result == {"ok": True}
    _assert_no_secret(caplog.text)
    assert "rate-limited" in caplog.text
    assert "redacted" in caplog.text


async def test_signed_url_transport_error_no_secret_leak(
    httpx_mock, caplog
) -> None:
    """A transport error whose message embeds the signed URL is scrubbed."""
    # httpx renders the request URL into the exception; craft one that carries it.
    request = httpx.Request("GET", _SIGNED_URL)
    for _ in range(3):
        httpx_mock.add_exception(
            httpx.ConnectError("connection failed", request=request)
        )
    sleep = RecordingSleep()

    with caplog.at_level(logging.WARNING, logger="trading_bot.transport.http"):
        async with AsyncHTTPClient(max_retries=3, sleep=sleep) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(_SIGNED_URL)

    _assert_no_secret(caplog.text)


async def test_signed_url_no_retry_5xx_ambiguous_no_secret_leak(
    httpx_mock, caplog
) -> None:
    """A ``retry=False`` 5xx on a signed URL → ambiguous error, no secret leak."""
    httpx_mock.add_response(status_code=503, text="upstream down")
    sleep = RecordingSleep()

    with caplog.at_level(logging.DEBUG, logger="trading_bot.transport.http"):
        async with AsyncHTTPClient(sleep=sleep) as client:
            with pytest.raises(AmbiguousRequestError) as exc_info:
                await client.request("POST", _SIGNED_URL, retry=False)

    _assert_no_secret(str(exc_info.value))
    _assert_no_secret(caplog.text)
    assert "reconcile" in str(exc_info.value)


async def test_signed_url_no_retry_transport_error_ambiguous_no_secret_leak(
    httpx_mock, caplog
) -> None:
    """A ``retry=False`` transport error embeds the URL in ``reason``, scrubbed."""
    request = httpx.Request("POST", _SIGNED_URL)
    httpx_mock.add_exception(
        httpx.ConnectError("connection failed", request=request)
    )
    sleep = RecordingSleep()

    with caplog.at_level(logging.DEBUG, logger="trading_bot.transport.http"):
        async with AsyncHTTPClient(sleep=sleep) as client:
            with pytest.raises(AmbiguousRequestError) as exc_info:
                await client.request("POST", _SIGNED_URL, retry=False)

    _assert_no_secret(str(exc_info.value))
    _assert_no_secret(caplog.text)


# --- B-11: transport hardening (pool, timeouts, size cap, redirects) ------- #


async def test_client_config_pool_timeouts_no_redirect_and_trust_env() -> None:
    """The underlying httpx client is built with explicit hardening config.

    B-11: bounded connection pool (``Limits``), per-*phase* timeout (connect vs
    read distinguished, not one scalar), ``follow_redirects=False`` (a signed
    query must never be replayed to another host), and an explicit — default
    off — ``trust_env`` proxy posture.
    """
    async with AsyncHTTPClient(
        timeout=10.0, connect_timeout=3.0, read_timeout=7.0
    ) as client:
        # Bounded pool (not httpx's unbounded default) — the configured limits.
        assert client._limits.max_connections == _MAX_CONNECTIONS
        assert (
            client._limits.max_keepalive_connections
            == _MAX_KEEPALIVE_CONNECTIONS
        )

        httpx_client = client._client
        assert httpx_client is not None

        # Per-phase timeouts: connect and read are DISTINCT, not one scalar.
        timeout = httpx_client.timeout
        assert timeout.connect == pytest.approx(3.0)
        assert timeout.read == pytest.approx(7.0)
        assert timeout.connect != timeout.read

        # A signed API client must not follow a redirect (could replay the
        # signed query to another host).
        assert httpx_client.follow_redirects is False
        # Explicit proxy/TLS-env posture, off by default.
        assert httpx_client.trust_env is False


async def test_read_timeout_on_submit_stays_ambiguous_no_retry(
    httpx_mock,
) -> None:
    """A **read**-timeout on a ``retry=False`` submit → ambiguous, sent once.

    A read-timeout means the request WAS sent but the response was lost — the
    order may have landed. It must stay ambiguous (reconcile) and must NEVER be
    auto-retried (a retry could place a second order). This is the core B-11 /
    idempotency guarantee.
    """
    request = httpx.Request("POST", "https://example.test/AddOrder")
    httpx_mock.add_exception(httpx.ReadTimeout("read timed out", request=request))
    sleep = RecordingSleep()

    async with AsyncHTTPClient(max_retries=3, sleep=sleep) as client:
        with pytest.raises(AmbiguousRequestError) as exc_info:
            await client.post(
                "https://example.test/AddOrder",
                data={"pair": "XBTUSD"},
                retry=False,
            )

    # Sent exactly once, NEVER retried, no backoff sleep.
    assert len(httpx_mock.get_requests()) == 1
    assert sleep.calls == []
    msg = str(exc_info.value)
    assert "reconcile" in msg
    # The reason names the read-timeout phase (request sent, outcome unknown).
    assert "read-timeout" in exc_info.value.reason


async def test_connect_timeout_on_submit_stays_ambiguous_no_retry(
    httpx_mock,
) -> None:
    """A **connect**-timeout on a ``retry=False`` submit is also ambiguous.

    A connect-timeout most likely never left, but the transport stays
    conservative: it surfaces ambiguous (reconcile) rather than risk a double
    submit on a mis-classified phase. The reason names the connect phase so the
    two cases are still distinguishable in logs.
    """
    request = httpx.Request("POST", "https://example.test/AddOrder")
    httpx_mock.add_exception(
        httpx.ConnectTimeout("connect timed out", request=request)
    )
    sleep = RecordingSleep()

    async with AsyncHTTPClient(max_retries=3, sleep=sleep) as client:
        with pytest.raises(AmbiguousRequestError) as exc_info:
            await client.post(
                "https://example.test/AddOrder",
                data={"pair": "XBTUSD"},
                retry=False,
            )

    assert len(httpx_mock.get_requests()) == 1
    assert sleep.calls == []
    assert "connect-timeout" in exc_info.value.reason


async def test_read_timeout_get_is_retried(httpx_mock) -> None:
    """An idempotent GET read-timeout is still retried (unchanged behaviour).

    Contrast with the non-idempotent submit above: a read-timeout is a transient
    transport error, so the *idempotent* path retries it with backoff and can
    recover — only the non-idempotent ``retry=False`` path must stay ambiguous.
    """
    request = httpx.Request("GET", "https://example.test/flaky")
    httpx_mock.add_exception(httpx.ReadTimeout("read timed out", request=request))
    httpx_mock.add_response(status_code=200, json={"recovered": True})
    sleep = RecordingSleep()

    async with AsyncHTTPClient(sleep=sleep) as client:
        result = await client.get("https://example.test/flaky")

    assert result == {"recovered": True}
    assert sleep.calls == [pytest.approx(0.5)]


async def test_response_size_cap_rejects_oversized_declared_body(
    httpx_mock,
) -> None:
    """A response declaring an over-cap ``Content-Length`` is refused.

    B-11: the body is rejected with :class:`ResponseTooLargeError` before it is
    trusted / parsed, guarding against a runaway or hostile payload.
    """
    httpx_mock.add_response(
        status_code=200,
        json={"ok": True},
        headers={"Content-Length": str(_MAX_RESPONSE_BYTES + 1)},
    )

    async with AsyncHTTPClient() as client:
        with pytest.raises(ResponseTooLargeError) as exc_info:
            await client.get("https://example.test/huge")

    assert exc_info.value.limit == _MAX_RESPONSE_BYTES


async def test_response_size_cap_rejects_oversized_actual_body(
    httpx_mock,
) -> None:
    """A body larger than the cap is refused even without a declared length.

    The authoritative check is the actually-buffered byte length, so a missing
    or lying ``Content-Length`` cannot smuggle an over-cap body through.
    """
    oversized = b"x" * (_MAX_RESPONSE_BYTES + 1)
    httpx_mock.add_response(status_code=200, content=oversized)

    async with AsyncHTTPClient() as client:
        with pytest.raises(ResponseTooLargeError):
            await client.get("https://example.test/huge-actual")


async def test_response_under_cap_parses_normally(httpx_mock) -> None:
    """A normal (under-cap) response still parses to JSON unchanged."""
    httpx_mock.add_response(status_code=200, json={"balances": {"USD": "10"}})

    async with AsyncHTTPClient() as client:
        result = await client.get("https://example.test/balances")

    assert result == {"balances": {"USD": "10"}}


@pytest.mark.network
async def test_real_kraken_time() -> None:
    """Live GET against Kraken's public Time endpoint (opt-in: ``-m network``)."""
    async with AsyncHTTPClient() as client:
        payload = await client.get("https://api.kraken.com/0/public/Time")

    assert isinstance(payload, dict)
    unixtime = payload["result"]["unixtime"]
    assert isinstance(unixtime, int)
    assert unixtime > 0
