"""Async rate-limiting primitives for the execution layer.

Two complementary models, both pure-stdlib and **venue-neutral plumbing** (no
I/O, no network, no domain business logic):

* :class:`TokenBucket` / :class:`RateLimiter` — *proactive* per-exchange
  throttling. A single :class:`RateLimiter` (one :class:`TokenBucket` per
  exchange) smooths concurrent operations on the same venue to its published
  request rate, instead of each caller firing at the full rate independently.
  Mirrors ``dccd.transport.ratelimit``;
  :class:`~trading_bot.transport.http.AsyncHTTPClient` keeps its reactive
  429/Retry-After handling as a backstop.

* :class:`KrakenCallCounter` — Kraken's private-endpoint **decaying call
  counter**. Each private call adds a per-endpoint *cost*; the counter decays
  by one every ``time_down`` seconds; :meth:`KrakenCallCounter.acquire` waits
  until adding the next cost would not exceed ``call_rate_limit``. The model
  (tier constants and per-endpoint costs) is ported from the legacy
  ``trading_bot/legacy/tools/call_counters.py`` — see :data:`_KRAKEN_TIERS` and
  :data:`KrakenCallCounter.COSTS`.

Default proactive rates are deliberately conservative (public, unauthenticated
REST):

============  =========  ====================================================
exchange      req/s      source
============  =========  ====================================================
binance       10.0       weight-based, 1200 weight/min; klines weight 2 → high
coinbase       3.0       public endpoints 3 req/s (docs.cdp.coinbase.com)
kraken         1.0       public endpoints ~1 req/s (support.kraken.com)
bybit         10.0       public market data ~10 req/s
okx            8.0       history-candles 20 req/2s = 10/s; kept under for margin
bitfinex       1.0       public REST 10-90 req/min → ~1 req/s conservative
bitmex         0.5       unauthenticated ~30 req/min → 0.5 req/s
============  =========  ====================================================

The clock and sleep are injected as seams (``time_source`` / ``sleep``,
matching the sibling :mod:`trading_bot.transport.http` and :mod:`~.ws` ``sleep``
seam) so timing and decay are deterministic under a fake clock in tests.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

__all__ = ["KrakenCallCounter", "RateLimiter", "TokenBucket"]

_DEFAULT_RATES: dict[str, float] = {
    "binance": 10.0,
    "coinbase": 3.0,
    "kraken": 1.0,
    "bybit": 10.0,
    "okx": 8.0,
    "bitfinex": 1.0,
    "bitmex": 0.5,
}

# Rate (req/s) used for any exchange not listed in ``_DEFAULT_RATES``.
_FALLBACK_RATE = 3.0


class TokenBucket:
    """Single token-bucket for one exchange.

    Tokens refill continuously at *rate* per second, capped at *capacity*.
    :meth:`acquire` consumes one token, waiting only when fewer than one is
    available. A caller slower than *rate* therefore never waits; a burst is
    smoothed to one request per ``1/rate`` seconds.

    Parameters
    ----------
    rate : float
        Sustained requests per second.
    capacity : float, optional
        Maximum tokens held (burst capacity). Defaults to *rate* (a one-second
        burst), matching dccd.
    time_source : callable, optional
        Monotonic time source (seconds). Injected as a seam for deterministic
        tests; defaults to :func:`time.monotonic`.
    sleep : callable, optional
        ``asyncio.sleep``-compatible coroutine used to wait for a token.
        Injected as a seam so timing is testable without real waits; defaults
        to :func:`asyncio.sleep`.
    """

    def __init__(
        self,
        rate: float,
        *,
        capacity: float | None = None,
        time_source: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if rate <= 0.0:
            raise ValueError(f"rate must be positive, got {rate!r}")
        self._rate = rate
        self._capacity = rate if capacity is None else capacity
        self._time_source = time_source
        self._sleep = sleep
        self._tokens = self._capacity
        self._last = time_source()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Add tokens accrued since the last update, capped at capacity."""
        now = self._time_source()
        elapsed = now - self._last
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last = now

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it.

        Serialised by an :class:`asyncio.Lock` so concurrent callers on the
        same bucket draw tokens one at a time (no double-spend on refill).
        """
        async with self._lock:
            self._refill()
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await self._sleep(wait)
                # Re-refill against the (possibly faked) clock after waiting,
                # then spend: under a real clock ``wait`` seconds have passed,
                # so exactly one token is now available.
                self._refill()
            self._tokens -= 1.0


class RateLimiter:
    """Per-exchange rate limiter holding one :class:`TokenBucket` each.

    Buckets are created lazily on first use of an exchange, so distinct
    exchanges throttle independently. The seams are forwarded to every bucket.

    Parameters
    ----------
    rates : dict of str to float, optional
        Map of exchange name → requests per second, merged over the
        conservative defaults. Unknown exchanges fall back to
        ``_FALLBACK_RATE``.
    time_source : callable, optional
        Monotonic time source forwarded to each bucket (test seam); defaults
        to :func:`time.monotonic`.
    sleep : callable, optional
        ``asyncio.sleep``-compatible coroutine forwarded to each bucket (test
        seam); defaults to :func:`asyncio.sleep`.

    Notes
    -----
    Satisfies the structural ``_Limiter`` protocol expected by
    :class:`~trading_bot.transport.http.AsyncHTTPClient`
    (``async acquire(exchange: str | None) -> None``). Calling
    :meth:`acquire` with ``None`` is a deliberate no-op: a request with no
    exchange key cannot be attributed to a bucket, so it is not throttled
    (the HTTP client only passes ``None`` when no *exchange* was configured).
    """

    def __init__(
        self,
        rates: dict[str, float] | None = None,
        *,
        time_source: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._rates = {**_DEFAULT_RATES, **(rates or {})}
        self._time_source = time_source
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}

    def _bucket(self, exchange: str) -> TokenBucket:
        bucket = self._buckets.get(exchange)
        if bucket is None:
            rate = self._rates.get(exchange, _FALLBACK_RATE)
            bucket = TokenBucket(
                rate, time_source=self._time_source, sleep=self._sleep
            )
            self._buckets[exchange] = bucket
        return bucket

    async def acquire(self, exchange: str | None) -> None:
        """Wait until a token is available for *exchange*, then consume it.

        Parameters
        ----------
        exchange : str or None
            Exchange key selecting the bucket. ``None`` is a no-op (an
            unattributed request is not throttled).
        """
        if exchange is None:
            return
        await self._bucket(exchange).acquire()

    @asynccontextmanager
    async def __call__(self, exchange: str) -> AsyncIterator[None]:
        """Async context manager that acquires a token on enter."""
        await self.acquire(exchange)
        yield


# Kraken private-endpoint tiers: (time_down seconds, call_rate_limit).
# Ported verbatim from ``trading_bot/legacy/tools/call_counters.py``
# (``KrakenCallCounter.__init__``): the counter decays by 1 every ``time_down``
# seconds and a user is banned at ``call_rate_limit``.
_KRAKEN_TIERS: dict[str, tuple[int, int]] = {
    "starter": (3, 15),
    "intermediate": (2, 20),
    "pro": (1, 20),
}


class KrakenCallCounter:
    """Kraken private-endpoint decaying call counter.

    Models Kraken's API counter for private endpoints: each call adds a
    per-endpoint *cost* (:data:`COSTS`); the counter decays by one every
    ``time_down`` seconds; a user's account is banned once the counter reaches
    ``call_rate_limit``. :meth:`acquire` waits until adding the next cost would
    keep the counter strictly under the limit (leaving a one-unit safety margin,
    as the legacy code did).

    Constants are ported from ``trading_bot/legacy/tools/call_counters.py``
    (the legacy ``_CallCounter`` / ``KrakenCallCounter``): see
    :data:`_KRAKEN_TIERS` for the per-tier ``(time_down, call_rate_limit)`` and
    :data:`COSTS` for the per-endpoint cost table.

    Parameters
    ----------
    time_down : int
        Seconds elapsed per one-unit decay of the counter.
    call_rate_limit : int
        Counter value at which the account is banned (calls are paced to stay
        under it).
    time_source : callable, optional
        Monotonic time source (seconds). Injected as a seam for deterministic
        tests; defaults to :func:`time.monotonic`.
    sleep : callable, optional
        ``asyncio.sleep``-compatible coroutine used to wait out the counter.
        Injected as a seam so timing is testable without real waits; defaults
        to :func:`asyncio.sleep`.

    Notes
    -----
    The legacy model worked in integer wall-clock seconds and decayed by
    ``(t - last) // time_down``; this port keeps the same integer-step decay
    against the (monotonic) seam so the asserted timeline matches the legacy
    numbers exactly.
    """

    #: Per-endpoint counter cost, ported from
    #: ``KrakenCallCounter._handler_method`` in the legacy module.
    COSTS: dict[str, int] = {
        "AddOrder": 0,
        "CancelOrder": 0,
        "Balance": 1,
        "TradeBalance": 1,
        "OpenOrders": 1,
        "ClosedOrders": 1,
        "QueryOrders": 1,
        "QueryTrades": 1,
        "OpenPositions": 1,
        "TradeVolume": 1,
        "GetWebSocketsToken": 1,
        "AddExport": 1,
        "ExportStatus": 1,
        "RetrieveExport": 1,
        "RemoveExport": 1,
        "TradesHistory": 2,
        "Ledgers": 2,
        "QueryLedgers": 2,
    }

    def __init__(
        self,
        *,
        time_down: int,
        call_rate_limit: int,
        time_source: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if time_down <= 0:
            raise ValueError(f"time_down must be positive, got {time_down!r}")
        if call_rate_limit <= 0:
            raise ValueError(
                f"call_rate_limit must be positive, got {call_rate_limit!r}"
            )
        self.time_down = time_down
        self.call_rate_limit = call_rate_limit
        self._time_source = time_source
        self._sleep = sleep
        self.counter = 0
        self._last = int(time_source())
        self._lock = asyncio.Lock()

    @classmethod
    def for_tier(
        cls,
        tier: str,
        *,
        time_source: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> KrakenCallCounter:
        """Build a counter for a verified-user *tier*.

        Parameters
        ----------
        tier : {'starter', 'intermediate', 'pro'}
            Kraken account verification tier. Selects the
            ``(time_down, call_rate_limit)`` pair from :data:`_KRAKEN_TIERS`.
        time_source, sleep : callable, optional
            Seams forwarded to the constructor.

        Returns
        -------
        KrakenCallCounter
        """
        try:
            time_down, call_rate_limit = _KRAKEN_TIERS[tier.lower()]
        except KeyError:
            raise ValueError(f"Invalid tier {tier!r}") from None
        return cls(
            time_down=time_down,
            call_rate_limit=call_rate_limit,
            time_source=time_source,
            sleep=sleep,
        )

    @classmethod
    def cost_of(cls, method: str) -> int:
        """Return the counter cost of a private *method*.

        Parameters
        ----------
        method : str
            Kraken private endpoint name (e.g. ``"AddOrder"``).

        Returns
        -------
        int
            The per-call counter cost.

        Raises
        ------
        ValueError
            If *method* is not a known Kraken private endpoint.
        """
        cost = cls.COSTS.get(method)
        if cost is None:
            raise ValueError(f"Unknown method {method!r}")
        return cost

    def _decay(self) -> None:
        """Decay the counter for time elapsed since the last update.

        Mirrors the legacy integer-step decay: ``(now - last) // time_down``
        units are removed, the clock anchor advances by whole steps only, and
        the counter floors at zero.
        """
        now = int(self._time_source())
        steps = (now - self._last) // self.time_down
        if steps > 0:
            self.counter = max(self.counter - steps, 0)
            self._last += steps * self.time_down

    def current(self) -> int:
        """Return the counter value after decaying to the current time.

        Returns
        -------
        int
            The decayed counter (a side-effecting read: it advances the decay
            anchor, exactly as the legacy ``__call__`` did).
        """
        self._decay()
        return self.counter

    def would_exceed(self, cost: int) -> bool:
        """Whether adding *cost* now would force :meth:`acquire` to wait.

        Decays to the current time first, then reports whether
        ``counter + cost`` would land **past** the ``call_rate_limit - 1``
        safety margin (the one-unit margin the legacy code kept). A call that
        lands exactly on the margin is still admitted without waiting, so this
        returns ``True`` only when the counter would be pushed strictly above
        it — i.e. exactly when :meth:`acquire` would sleep.

        Parameters
        ----------
        cost : int
            Counter cost of the prospective call.

        Returns
        -------
        bool
            ``True`` if the call would wait before proceeding.
        """
        self._decay()
        return self._wait_for(cost) > 0.0

    def _wait_for(self, cost: int) -> float:
        """Seconds to wait so that ``counter + cost`` clears the margin.

        Assumes the counter has just been decayed. Returns ``0.0`` when the
        call already fits.
        """
        overshoot = self.counter + cost - (self.call_rate_limit - 1)
        if overshoot <= 0:
            return 0.0
        # Each ``time_down`` seconds removes one unit; wait whole decay steps.
        return float(overshoot * self.time_down)

    async def acquire(self, cost: int) -> None:
        """Account for a call of *cost*, waiting if it would breach the limit.

        Decays the counter to now; if adding *cost* would reach the
        ``call_rate_limit - 1`` margin, waits exactly long enough for the
        counter to decay below it, then records the cost.

        Parameters
        ----------
        cost : int
            Counter cost of the call (see :meth:`cost_of` / :data:`COSTS`).
        """
        async with self._lock:
            self._decay()
            wait = self._wait_for(cost)
            if wait > 0.0:
                await self._sleep(wait)
                self._decay()
            self.counter += cost

    async def acquire_method(self, method: str) -> None:
        """Convenience: :meth:`acquire` the cost of a named private *method*.

        Parameters
        ----------
        method : str
            Kraken private endpoint name (e.g. ``"TradesHistory"``).
        """
        await self.acquire(self.cost_of(method))
