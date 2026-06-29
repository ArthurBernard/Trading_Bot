"""Tests for :mod:`trading_bot.transport.ratelimit`.

Every test drives the timing layer through injected ``time_source`` / ``sleep``
seams: a :class:`FakeClock` advances *only* when the recording :class:`Clock`-
bound sleep is awaited (and never on its own), so the spacing and decay are
asserted deterministically with no real waits. The Kraken counter assertions
cite the legacy constants ported in :mod:`trading_bot.transport.ratelimit`.
"""

from __future__ import annotations

import pytest

from trading_bot.transport import KrakenCallCounter, RateLimiter, TokenBucket


class FakeClock:
    """A monotonic clock that only moves when its :attr:`sleep` is awaited.

    Used as both the ``time_source`` (read :attr:`now`) and the ``sleep`` seam
    (advances :attr:`now` by the requested delay and records it). This couples
    "time passed" to "we slept", so a token-bucket wait is reflected in the
    next refill exactly as it would be under a real clock.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.waits.append(delay)
        self.now += delay

    def advance(self, seconds: float) -> None:
        """Advance the clock without sleeping (simulate a slow caller)."""
        self.now += seconds

    @property
    def total_waited(self) -> float:
        return sum(self.waits)


# --------------------------------------------------------------------------- #
# TokenBucket
# --------------------------------------------------------------------------- #


async def test_burst_is_spaced_to_rate() -> None:
    # rate=2/s, capacity=2 (default). A burst of acquires beyond the initial
    # capacity is paced to one token every 1/rate = 0.5s.
    clock = FakeClock()
    bucket = TokenBucket(2.0, time_source=clock.time, sleep=clock.sleep)

    # First two consume the initial burst capacity with no wait.
    await bucket.acquire()
    await bucket.acquire()
    assert clock.waits == []

    # The next three each wait one inter-token interval (0.5s).
    for _ in range(3):
        await bucket.acquire()

    assert clock.waits == [
        pytest.approx(0.5),
        pytest.approx(0.5),
        pytest.approx(0.5),
    ]
    assert clock.total_waited == pytest.approx(1.5)


async def test_slow_caller_never_waits() -> None:
    # A caller slower than the rate (1/s here) always finds a token ready.
    clock = FakeClock()
    bucket = TokenBucket(1.0, time_source=clock.time, sleep=clock.sleep)

    await bucket.acquire()  # spends the one-token initial burst
    for _ in range(5):
        clock.advance(1.0)  # one full inter-token interval between calls
        await bucket.acquire()

    assert clock.waits == []


async def test_capacity_overrides_burst() -> None:
    # An explicit capacity sets the burst size independently of the rate.
    clock = FakeClock()
    bucket = TokenBucket(
        10.0, capacity=1.0, time_source=clock.time, sleep=clock.sleep
    )

    await bucket.acquire()  # spends the single token of capacity
    await bucket.acquire()  # must wait one interval: 1/rate = 0.1s

    assert clock.waits == [pytest.approx(0.1)]


def test_non_positive_rate_rejected() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0.0)


# --------------------------------------------------------------------------- #
# RateLimiter
# --------------------------------------------------------------------------- #


async def test_distinct_exchanges_use_independent_buckets() -> None:
    clock = FakeClock()
    limiter = RateLimiter(time_source=clock.time, sleep=clock.sleep)

    # kraken default rate is 1/s (capacity 1): one free, the second waits 1s.
    await limiter.acquire("kraken")
    await limiter.acquire("kraken")
    assert clock.waits == [pytest.approx(1.0)]

    # A *different* exchange has its own full bucket: the first binance acquire
    # does not wait, proving the buckets are independent.
    clock.waits.clear()
    await limiter.acquire("binance")
    assert clock.waits == []


async def test_unknown_exchange_uses_fallback_rate() -> None:
    # Fallback rate is 3/s (capacity 3): three free, the fourth waits 1/3 s.
    clock = FakeClock()
    limiter = RateLimiter(time_source=clock.time, sleep=clock.sleep)

    for _ in range(3):
        await limiter.acquire("nasdaq-totally-unknown")
    assert clock.waits == []

    await limiter.acquire("nasdaq-totally-unknown")
    assert clock.waits == [pytest.approx(1.0 / 3.0)]


async def test_custom_rates_merge_over_defaults() -> None:
    clock = FakeClock()
    limiter = RateLimiter(
        {"kraken": 4.0}, time_source=clock.time, sleep=clock.sleep
    )

    # Overridden kraken rate 4/s (capacity 4): four free, fifth waits 0.25s.
    for _ in range(4):
        await limiter.acquire("kraken")
    assert clock.waits == []
    await limiter.acquire("kraken")
    assert clock.waits == [pytest.approx(0.25)]


async def test_acquire_none_is_noop() -> None:
    clock = FakeClock()
    limiter = RateLimiter(time_source=clock.time, sleep=clock.sleep)

    # None means "unattributed request": never throttled, no bucket created.
    for _ in range(100):
        await limiter.acquire(None)
    assert clock.waits == []


async def test_context_manager_acquires() -> None:
    clock = FakeClock()
    limiter = RateLimiter(time_source=clock.time, sleep=clock.sleep)

    async with limiter("kraken"):
        pass
    async with limiter("kraken"):  # second entry waits one kraken interval
        pass
    assert clock.waits == [pytest.approx(1.0)]


# --------------------------------------------------------------------------- #
# KrakenCallCounter
# --------------------------------------------------------------------------- #


def test_costs_match_legacy_table() -> None:
    # Ported verbatim from legacy KrakenCallCounter._handler_method.
    assert KrakenCallCounter.cost_of("AddOrder") == 0
    assert KrakenCallCounter.cost_of("CancelOrder") == 0
    assert KrakenCallCounter.cost_of("Balance") == 1
    assert KrakenCallCounter.cost_of("OpenOrders") == 1
    assert KrakenCallCounter.cost_of("TradesHistory") == 2
    assert KrakenCallCounter.cost_of("Ledgers") == 2
    assert KrakenCallCounter.cost_of("QueryLedgers") == 2
    with pytest.raises(ValueError):
        KrakenCallCounter.cost_of("NotAMethod")


def test_every_private_method_the_broker_calls_has_a_cost() -> None:
    """Each private endpoint `KrakenBroker` / the private WS posts must be costed.

    Regression for the live-validation finding: `GetWebSocketsToken` was missing
    from the cost table, so `cost_of` raised "Unknown method" and the private
    executions WebSocket could never fetch a token (it was wholly non-functional).
    Every signed endpoint the engine actually calls must be in `COSTS`.
    """
    for method in (
        "AddOrder",
        "CancelOrder",
        "Balance",
        "OpenOrders",
        "TradesHistory",
        "GetWebSocketsToken",  # the WS token endpoint — was missing
    ):
        KrakenCallCounter.cost_of(method)  # must not raise
    assert KrakenCallCounter.cost_of("GetWebSocketsToken") == 1


def test_tiers_match_legacy_constants() -> None:
    # Legacy KrakenCallCounter.__init__: starter (3, 15), intermediate (2, 20),
    # pro (1, 20).
    starter = KrakenCallCounter.for_tier("starter")
    assert (starter.time_down, starter.call_rate_limit) == (3, 15)
    intermediate = KrakenCallCounter.for_tier("INTERMEDIATE")
    assert (intermediate.time_down, intermediate.call_rate_limit) == (2, 20)
    pro = KrakenCallCounter.for_tier("pro")
    assert (pro.time_down, pro.call_rate_limit) == (1, 20)
    with pytest.raises(ValueError):
        KrakenCallCounter.for_tier("oligarch")


async def test_counter_increments_per_cost() -> None:
    clock = FakeClock()
    counter = KrakenCallCounter(
        time_down=3, call_rate_limit=15, time_source=clock.time, sleep=clock.sleep
    )

    await counter.acquire_method("Balance")  # +1
    assert counter.current() == 1
    await counter.acquire_method("TradesHistory")  # +2
    assert counter.current() == 3
    await counter.acquire_method("AddOrder")  # +0
    assert counter.current() == 3
    assert clock.waits == []


async def test_counter_decays_over_time() -> None:
    # Legacy decay: counter drops by (elapsed // time_down). With time_down=3,
    # 9 seconds removes 3 units.
    clock = FakeClock()
    counter = KrakenCallCounter(
        time_down=3, call_rate_limit=15, time_source=clock.time, sleep=clock.sleep
    )

    for _ in range(5):
        await counter.acquire_method("Balance")  # counter = 5
    assert counter.current() == 5

    clock.advance(9.0)  # 9 // 3 = 3 units decay
    assert counter.current() == 2

    clock.advance(100.0)  # floors at zero
    assert counter.current() == 0


async def test_acquire_waits_when_limit_would_be_exceeded() -> None:
    # starter tier: time_down=3, call_rate_limit=15. Margin = call_rate_limit
    # - 1 = 14. A call lands without waiting up to and *onto* the margin (14);
    # it waits only when it would be pushed strictly past it. The correct model
    # waits ``overshoot * time_down`` so the counter actually decays clear.
    clock = FakeClock()
    counter = KrakenCallCounter(
        time_down=3, call_rate_limit=15, time_source=clock.time, sleep=clock.sleep
    )

    # Drive the counter exactly onto the margin (14) with Balance calls (+1) —
    # none wait, since 13 + 1 == 14 lands on the margin, not past it.
    for _ in range(14):
        await counter.acquire_method("Balance")
    assert counter.current() == 14
    assert clock.waits == []

    # The 15th +1 would make 15 > 14 → overshoot 1 → wait 1 * time_down = 3s.
    await counter.acquire_method("Balance")
    assert clock.waits == [pytest.approx(3.0)]
    # 3s decays 1 unit: 14 - 1 = 13, then + 1 newly added = 14.
    assert counter.current() == 14


def test_would_exceed_reports_margin() -> None:
    clock = FakeClock()
    counter = KrakenCallCounter(
        time_down=3, call_rate_limit=15, time_source=clock.time, sleep=clock.sleep
    )
    counter.counter = 13
    counter._last = int(clock.time())
    assert counter.would_exceed(1) is False  # 14 lands on margin → admitted
    assert counter.would_exceed(2) is True  # 15 > 14 → would wait


def test_non_positive_params_rejected() -> None:
    with pytest.raises(ValueError):
        KrakenCallCounter(time_down=0, call_rate_limit=15)
    with pytest.raises(ValueError):
        KrakenCallCounter(time_down=3, call_rate_limit=0)


# --------------------------------------------------------------------------- #
# Verification on real data: realistic Kraken private-call burst (fake clock)
# --------------------------------------------------------------------------- #


async def test_realistic_kraken_private_burst_timeline() -> None:
    """Drive a realistic burst of Kraken private calls under a fake clock.

    Tier 'starter' (time_down=3, call_rate_limit=15, margin=14). The pattern
    front-loads heavy history pulls then settles into balance polls; we assert
    the exact wait timeline produced by the decaying counter.
    """
    clock = FakeClock()
    counter = KrakenCallCounter.for_tier(
        "starter", time_source=clock.time, sleep=clock.sleep
    )

    # 6 TradesHistory (cost 2 each) → counter climbs 2,4,6,8,10,12. None hit
    # the 14 margin (12 + 2 = 14 would, so the 7th is where it bites).
    timeline: list[tuple[str, int, float]] = []
    for _ in range(6):
        await counter.acquire_method("TradesHistory")
        timeline.append(("TradesHistory", counter.counter, clock.now))

    assert [c for _, c, _ in timeline] == [2, 4, 6, 8, 10, 12]
    assert clock.waits == []  # nothing waited yet

    # 7th TradesHistory: 12 + 2 = 14 >= margin → wait. Overshoot = 14 - 14 = 0?
    # No: margin is call_rate_limit - 1 = 14, so counter + cost >= 14 holds.
    # overshoot = 12 + 2 - 14 = 0 → no wait; it lands exactly on the margin and
    # is allowed (the legacy code sleeps only when strictly past, see counter
    # value). Confirm it proceeds without waiting and sits at 14.
    await counter.acquire_method("TradesHistory")
    assert counter.counter == 14
    assert clock.waits == []

    # 8th TradesHistory: 14 + 2 - 14 = 2 overshoot → wait 2 * time_down = 6s.
    await counter.acquire_method("TradesHistory")
    assert clock.waits == [pytest.approx(6.0)]
    # 6s decays 2 units: 14 - 2 = 12, then + 2 = 14.
    assert counter.counter == 14
    assert clock.now == pytest.approx(6.0)
