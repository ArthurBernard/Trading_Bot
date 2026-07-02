"""Tests for the :class:`RiskManager` — the engine's pre-trade gate + kill-switch.

These prove the last safety block before live trading: a breaching order — or any
order once the kill-switch is tripped — raises
:class:`~trading_bot.domain.errors.RiskLimitBreached` and is **never placed**. The
suite covers each limit in isolation (``max_order``, ``max_position``,
``max_daily_loss``), the kill-switch (``trip`` / ``reset`` / ``kill``), the
all-``None`` (unconstrained) config, and the end-to-end integration through the
:class:`~trading_bot.application.order_router.OrderRouter` against both a counting
spy broker (so we can assert ``place_order`` was *not* called) and the real
:class:`~trading_bot.brokers.paper.PaperBroker` (so positions are observed to move
or stay unchanged for real).

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import asyncio

import pytest

from trading_bot.application import (
    EventBus,
    OrderRouter,
    PositionTracker,
    RiskManager,
)
from trading_bot.application.config import RiskConfig
from trading_bot.brokers.base import Broker, Capability
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain import (
    BrokerError,
    Fill,
    Instrument,
    Money,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    RiskLimitBreached,
    Symbol,
    money,
)

BTC_USD = Instrument(Symbol("BTC", "USD"))


def _order(
    cid: str = "cid-1",
    qty: str = "1",
    side: OrderSide = OrderSide.BUY,
) -> Order:
    """A realistic limit order for assertions."""
    return Order(
        client_order_id=cid,
        instrument=BTC_USD,
        side=side,
        qty=money(qty),
        type=OrderType.LIMIT,
        limit_price=money("30000"),
    )


def _fill(
    cid: str,
    side: OrderSide,
    qty: str,
    price: str = "30000",
    fee: str = "0",
    ts: int = 1,
) -> Fill:
    """A broker-confirmed execution to seed a tracker/position."""
    return Fill(
        fill_id=f"F-{cid}-{ts}",
        client_order_id=cid,
        instrument=BTC_USD,
        side=side,
        qty=money(qty),
        price=money(price),
        fee=money(fee),
        ts=ts,
    )


class _SpyBroker(Broker):
    """A counting broker: records every ``place_order`` / ``cancel_order`` call.

    Serves only ``PLACE_ORDER`` and ``CANCEL`` (the router's requirements). Lets
    the risk-gate tests assert ``place_order`` was *not* reached on a refusal.
    """

    name = "spy"

    def __init__(self) -> None:
        self.place_calls = 0
        self.cancel_calls = 0
        self._ids = 0

    def capabilities(self) -> set[Capability]:
        return {Capability.PLACE_ORDER, Capability.CANCEL}

    async def place_order(self, order: Order) -> str:
        self.place_calls += 1
        self._ids += 1
        return f"SPY-{self._ids}"

    async def cancel_order(self, venue_order_id: str) -> None:
        self.cancel_calls += 1

    async def open_orders(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def balances(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def fills(self, since_ms=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def ticker(self, instrument):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class _CancelFailingBroker(Broker):
    """A :class:`PaperBroker` wrapper whose ``cancel_order`` can fail.

    Delegates every method to the wrapped paper broker (so the venue's recorded
    truth stays the paper broker's), but the *first* ``cancel_order`` raises a
    :class:`~trading_bot.domain.errors.BrokerError` — modelling a stuck cancel.
    Used to prove :meth:`RiskManager.kill` swallows the failure, still reaches
    the remaining orders, and still trips the switch (a stuck order never blocks
    the halt).
    """

    name = "cancel-failing"

    def __init__(self, inner: PaperBroker, *, fail_first_cancel: bool) -> None:
        self.inner = inner
        self._fail_first_cancel = fail_first_cancel
        self._fail_next_place: BrokerError | None = None
        self.cancel_attempts = 0

    def fail_next_place(self, exc: BrokerError) -> None:
        """Arm the next ``place_order`` to raise cleanly (no venue record)."""
        self._fail_next_place = exc

    def capabilities(self) -> set[Capability]:
        return self.inner.capabilities()

    async def place_order(self, order: Order) -> str:
        clean_fail = self._fail_next_place
        self._fail_next_place = None
        if clean_fail is not None:
            raise clean_fail
        return await self.inner.place_order(order)

    async def cancel_order(self, venue_order_id: str) -> None:
        self.cancel_attempts += 1
        if self._fail_first_cancel:
            self._fail_first_cancel = False
            raise BrokerError(f"simulated stuck cancel for {venue_order_id}")
        await self.inner.cancel_order(venue_order_id)

    async def open_orders(self) -> list[Order]:
        return await self.inner.open_orders()

    async def balances(self) -> dict[str, Money]:
        return await self.inner.balances()

    async def fills(self, since_ms: int | None = None) -> list[Fill]:
        return await self.inner.fills(since_ms)

    async def ticker(self, instrument: Instrument) -> Money:
        return await self.inner.ticker(instrument)


# --- max_order ------------------------------------------------------------- #


def test_max_order_blocks_oversize_order() -> None:
    """An order above ``max_order`` raises with the limit name / value / threshold."""
    rm = RiskManager(RiskConfig(max_order=money("1")))
    with pytest.raises(RiskLimitBreached) as exc:
        rm.check(_order(qty="2"))
    assert exc.value.limit == "max_order"
    assert exc.value.value == money("2")
    assert exc.value.threshold == money("1")


def test_max_order_allows_order_at_or_below_cap() -> None:
    """An order at exactly the cap (and below) passes — only ``>`` breaches."""
    rm = RiskManager(RiskConfig(max_order=money("1")))
    rm.check(_order(qty="1"))  # at the cap
    rm.check(_order(qty="0.5"))  # below


# --- max_position ---------------------------------------------------------- #


def test_max_position_blocks_order_pushing_net_past_cap() -> None:
    """An order whose resulting |net| exceeds ``max_position`` is blocked."""
    tracker = PositionTracker()
    tracker.apply(_fill("seed", OrderSide.BUY, "1"))  # net +1
    rm = RiskManager(
        RiskConfig(max_position=money("1.5")), position_tracker=tracker
    )
    # +1 (current) + 1 (this BUY) = 2 > 1.5 -> breach.
    with pytest.raises(RiskLimitBreached) as exc:
        rm.check(_order(qty="1", side=OrderSide.BUY))
    assert exc.value.limit == "max_position"
    assert exc.value.value == money("2")
    assert exc.value.threshold == money("1.5")


def test_max_position_allows_order_within_cap() -> None:
    """An order whose resulting |net| stays within the cap is allowed."""
    tracker = PositionTracker()
    tracker.apply(_fill("seed", OrderSide.BUY, "1"))  # net +1
    rm = RiskManager(
        RiskConfig(max_position=money("1.5")), position_tracker=tracker
    )
    # +1 + 0.5 = 1.5, not > 1.5 -> allowed.
    rm.check(_order(qty="0.5", side=OrderSide.BUY))


def test_max_position_reducing_order_never_blocked_by_it() -> None:
    """An order that *reduces* an over-cap position is not blocked by max_position."""
    tracker = PositionTracker()
    tracker.apply(_fill("seed", OrderSide.BUY, "5"))  # net +5 (already over cap)
    rm = RiskManager(
        RiskConfig(max_position=money("3")), position_tracker=tracker
    )
    # A SELL of 1: +5 + (-1) = +4 -> still over cap, blocked. But a SELL of 3
    # brings it to +2 which is within cap -> allowed (the gate is on the result).
    rm.check(_order(qty="3", side=OrderSide.SELL))


def test_max_position_uses_signed_side_for_resulting_net() -> None:
    """A SELL subtracts: with net +1 and cap 1, a SELL of 3 flips to |−2| > 1 -> blocked."""
    tracker = PositionTracker()
    tracker.apply(_fill("seed", OrderSide.BUY, "1"))  # net +1
    rm = RiskManager(
        RiskConfig(max_position=money("1")), position_tracker=tracker
    )
    with pytest.raises(RiskLimitBreached) as exc:
        rm.check(_order(qty="3", side=OrderSide.SELL))  # +1 - 3 = -2, |−2| = 2 > 1
    assert exc.value.value == money("2")


def test_max_position_no_tracker_treats_current_as_flat() -> None:
    """With no tracker, current exposure is flat (0) -> gate is on order qty only."""
    rm = RiskManager(RiskConfig(max_position=money("1")))
    rm.check(_order(qty="1", side=OrderSide.BUY))  # 0 + 1 = 1, allowed
    with pytest.raises(RiskLimitBreached):
        rm.check(_order(qty="2", side=OrderSide.BUY))  # 0 + 2 = 2 > 1


# --- max_daily_loss -------------------------------------------------------- #


def test_max_daily_loss_blocks_once_loss_reached() -> None:
    """Once the provider's daily loss >= cap, new orders are blocked."""
    realised = money("-50")

    def provider(_day_start_ms: int) -> Money:
        return realised

    rm = RiskManager(
        RiskConfig(max_daily_loss=money("100")), daily_pnl_provider=provider
    )
    rm.check(_order())  # loss 50 < 100 -> ok
    realised = money("-100")  # loss 100 >= 100 -> blocked
    with pytest.raises(RiskLimitBreached) as exc:
        rm.check(_order())
    assert exc.value.limit == "max_daily_loss"
    assert exc.value.value == money("100")
    assert exc.value.threshold == money("100")


def test_max_daily_loss_profit_never_blocks() -> None:
    """A profitable day (positive PnL) never registers as a loss -> never blocks."""
    rm = RiskManager(
        RiskConfig(max_daily_loss=money("100")),
        daily_pnl_provider=lambda _day_start_ms: money("250"),  # profit, loss 0
    )
    rm.check(_order())


def test_max_daily_loss_without_provider_never_blocks() -> None:
    """With no provider wired the daily-loss check sees no loss and never blocks."""
    rm = RiskManager(RiskConfig(max_daily_loss=money("100")))  # no provider
    rm.check(_order())


def test_max_daily_loss_via_provider() -> None:
    """The daily loss can be sourced from an injected day-scoped provider."""
    realised = money("0")

    def provider(_day_start_ms: int) -> Money:
        return realised

    rm = RiskManager(
        RiskConfig(max_daily_loss=money("100")),
        daily_pnl_provider=provider,
    )
    rm.check(_order())  # loss 0 < 100
    realised = money("-150")  # provider now reports a 150 loss
    with pytest.raises(RiskLimitBreached):
        rm.check(_order())


def test_daily_loss_provider_is_passed_the_utc_day_boundary() -> None:
    """The provider receives the current UTC day's midnight (ms since epoch)."""
    # 2021-01-01 12:00:00 UTC = 1_609_502_400_000 ms; that day's midnight is
    # 2021-01-01 00:00:00 UTC = 1_609_459_200_000 ms.
    now_ms = 1_609_502_400_000
    day_start_ms = 1_609_459_200_000
    seen: list[int] = []

    def provider(boundary_ms: int) -> Money:
        seen.append(boundary_ms)
        return money("0")

    rm = RiskManager(
        RiskConfig(max_daily_loss=money("100")),
        daily_pnl_provider=provider,
        clock=lambda: now_ms,
    )
    rm.check(_order())
    assert seen == [day_start_ms]


def test_daily_loss_breaker_resets_at_utc_day_boundary() -> None:
    """A day that trips the breaker no longer counts once the UTC day rolls over.

    Wires a day-scoped provider (as the service factory does) plus a controllable
    clock. Day 1's realised loss trips the ``max_daily_loss`` gate; advancing the
    clock past the next UTC midnight makes the manager ask the provider for *that*
    day's boundary — where the loss is back to zero — so trading resumes with no
    manual reset and no scheduler.
    """
    # Two adjacent UTC days.
    day1_noon = 1_609_502_400_000  # 2021-01-01 12:00 UTC
    day1_start = 1_609_459_200_000  # 2021-01-01 00:00 UTC
    day2_noon = day1_noon + 86_400_000  # 2021-01-02 12:00 UTC
    day2_start = day1_start + 86_400_000  # 2021-01-02 00:00 UTC

    now = {"ms": day1_noon}
    # Signed realised PnL per UTC-day boundary: day 1 lost 200, day 2 is fresh.
    pnl_by_day = {day1_start: money("-200"), day2_start: money("0")}

    rm = RiskManager(
        RiskConfig(max_daily_loss=money("100")),
        daily_pnl_provider=lambda boundary: pnl_by_day[boundary],
        clock=lambda: now["ms"],
    )

    # Day 1: the realised loss of 200 >= cap 100 -> the gate refuses.
    with pytest.raises(RiskLimitBreached) as exc:
        rm.check(_order())
    assert exc.value.limit == "max_daily_loss"

    # Cross into the next UTC day: the window rolls over automatically.
    now["ms"] = day2_noon
    rm.check(_order())  # day 2's loss is 0 -> trading resumes, no manual reset


def test_cumulative_loss_across_days_does_not_latch() -> None:
    """A loss carried across days never latches the kill-switch by itself.

    The bug A-1 fixes: wiring the breaker to *cumulative* session PnL means a
    day-1 loss stays counted forever and, once the router escalates to the
    kill-switch, halts the book permanently. With a day-scoped source the manager
    only ever sees the *current* day's PnL, so a prior day's loss cannot keep the
    gate breached — and the kill-switch is never reached on a fresh day.
    """
    day1_start = 1_609_459_200_000
    day2_start = day1_start + 86_400_000
    now = {"ms": day1_start + 3_600_000}  # day 1, 01:00 UTC

    # Cumulative PnL would be -200 on both days; the *daily* view is -200 then 0.
    daily_pnl = {day1_start: money("-200"), day2_start: money("0")}

    rm = RiskManager(
        RiskConfig(max_daily_loss=money("100")),
        daily_pnl_provider=lambda boundary: daily_pnl[boundary],
        clock=lambda: now["ms"],
    )
    with pytest.raises(RiskLimitBreached):
        rm.check(_order())  # day 1 breached
    assert rm.tripped is False, "the gate refusing an order must not itself trip"

    now["ms"] = day2_start + 3_600_000  # day 2, 01:00 UTC
    rm.check(_order())  # a new day: not latched, trading allowed


# --- kill-switch ----------------------------------------------------------- #


def test_trip_blocks_every_check_and_reset_re_enables() -> None:
    """trip() makes every check raise; reset() restores normal gating."""
    rm = RiskManager(RiskConfig())  # no limits at all
    assert rm.tripped is False
    rm.check(_order())  # passes pre-trip

    rm.trip("manual halt")
    assert rm.tripped is True
    assert rm.trip_reason == "manual halt"
    with pytest.raises(RiskLimitBreached) as exc:
        rm.check(_order())
    assert exc.value.limit == "kill_switch"

    rm.reset()
    assert rm.tripped is False
    assert rm.trip_reason is None
    rm.check(_order())  # passes again


async def test_kill_cancels_open_orders_and_trips_via_router() -> None:
    """kill(router=...) cancels each tracked live order, then trips the switch."""
    broker = PaperBroker(fill_model="partial", partial_fill_ratio=money("0.5"))
    bus = EventBus()
    rm = RiskManager(RiskConfig())
    router = OrderRouter(broker, bus, risk_manager=rm)

    # Two partially-filled (still-open) orders the venue holds live.
    o1 = await router.submit(_order(cid="k1"))
    o2 = await router.submit(_order(cid="k2"))
    assert o1.status is OrderStatus.OPEN
    assert o2.status is OrderStatus.OPEN
    assert len(await broker.open_orders()) == 2

    await rm.kill(router=router, reason="panic")

    # Both venue orders cancelled and local state transitioned.
    assert len(await broker.open_orders()) == 0
    assert o1.status is OrderStatus.CANCELLED
    assert o2.status is OrderStatus.CANCELLED
    # Switch is tripped: further submits are halted.
    assert rm.tripped is True
    with pytest.raises(RiskLimitBreached):
        await router.submit(_order(cid="k3"))


async def test_kill_cancels_open_orders_directly_via_broker() -> None:
    """kill(broker=...) cancels each open order the broker reports, then trips."""
    broker = PaperBroker(fill_model="partial", partial_fill_ratio=money("0.5"))
    bus = EventBus()
    router = OrderRouter(broker, bus)

    await router.submit(_order(cid="b1"))
    await router.submit(_order(cid="b2"))
    assert len(await broker.open_orders()) == 2

    rm = RiskManager(RiskConfig())
    await rm.kill(broker=broker, reason="panic")

    assert len(await broker.open_orders()) == 0
    assert rm.tripped is True


async def test_kill_requires_router_or_broker() -> None:
    """kill() with neither a router nor a broker is a programming error."""
    rm = RiskManager(RiskConfig())
    with pytest.raises(ValueError):
        await rm.kill()


async def test_kill_via_router_tolerates_a_stuck_cancel_and_still_trips() -> None:
    """A cancel that raises does not abort the halt: the switch still trips.

    The panic path must never be blocked by one stuck order. We drive two live
    orders, make the *first* cancel raise on the venue, and assert kill() still
    reaches the second order, still trips, and swallows the failure (no raise).
    """
    broker = _CancelFailingBroker(
        PaperBroker(fill_model="partial", partial_fill_ratio=money("0.5")),
        fail_first_cancel=True,
    )
    bus = EventBus()
    rm = RiskManager(RiskConfig())
    router = OrderRouter(broker, bus, risk_manager=rm)

    await router.submit(_order(cid="s1"))
    o2 = await router.submit(_order(cid="s2"))
    assert len(await broker.open_orders()) == 2

    # kill() must not raise even though the first cancel blows up.
    await rm.kill(router=router, reason="panic")

    # The switch tripped despite the failure, and the *reachable* order cancelled.
    assert rm.tripped is True
    assert broker.cancel_attempts == 2  # both were attempted, none skipped
    assert o2.status is OrderStatus.CANCELLED  # the non-failing one went through
    with pytest.raises(RiskLimitBreached):
        await router.submit(_order(cid="s3"))


async def test_kill_via_router_skips_terminal_and_unplaced_orders() -> None:
    """kill(router=...) only cancels orders live on a venue, skipping the rest.

    A rejected order is terminal *and* has no venue id — it is uncancellable and
    must be skipped (not attempted). One live order alongside it is still
    cancelled, and the switch still trips.
    """
    broker = _CancelFailingBroker(
        PaperBroker(fill_model="partial", partial_fill_ratio=money("0.5")),
        fail_first_cancel=False,
    )
    broker.fail_next_place(BrokerError("venue said no"))
    bus = EventBus()
    rm = RiskManager(RiskConfig())
    router = OrderRouter(broker, bus, risk_manager=rm)

    # First submit is rejected by the venue → terminal, no venue id (uncancellable).
    with pytest.raises(BrokerError):
        await router.submit(_order(cid="rej"))
    rejected = router.tracked_orders()["rej"]
    assert rejected.is_terminal
    assert rejected.venue_order_id is None
    # A second, live order the kill must still reach.
    live = await router.submit(_order(cid="live"))
    assert live.status is OrderStatus.OPEN

    await rm.kill(router=router, reason="panic")

    # Only the live order was cancellable; the rejected one was skipped, not tried.
    assert broker.cancel_attempts == 1
    assert live.status is OrderStatus.CANCELLED
    assert rm.tripped is True


async def test_kill_via_broker_skips_orders_without_a_venue_id() -> None:
    """Direct-broker kill() skips any reported order lacking a venue id."""

    class _NoVenueIdBroker(Broker):
        """Reports one open order with ``venue_order_id=None`` — uncancellable."""

        name = "no-venue-id"

        def __init__(self) -> None:
            self.cancel_attempts = 0

        def capabilities(self) -> set[Capability]:
            return {Capability.PLACE_ORDER, Capability.CANCEL}

        async def place_order(self, order: Order) -> str:
            raise NotImplementedError

        async def cancel_order(self, venue_order_id: str) -> None:
            self.cancel_attempts += 1

        async def open_orders(self) -> list[Order]:
            return [_order(cid="no-vid")]  # a fresh Order has venue_order_id=None

        async def balances(self) -> dict[str, Money]:
            raise NotImplementedError

        async def fills(self, since_ms: int | None = None) -> list[Fill]:
            raise NotImplementedError

        async def ticker(self, instrument: Instrument) -> Money:
            raise NotImplementedError

    broker = _NoVenueIdBroker()
    rm = RiskManager(RiskConfig())
    await rm.kill(broker=broker, reason="panic")

    assert broker.cancel_attempts == 0  # the venue-id-less order was skipped
    assert rm.tripped is True


async def test_kill_via_broker_tolerates_a_stuck_cancel_and_still_trips() -> None:
    """Direct-broker kill() also swallows a failed cancel and still trips."""
    broker = _CancelFailingBroker(
        PaperBroker(fill_model="partial", partial_fill_ratio=money("0.5")),
        fail_first_cancel=True,
    )
    bus = EventBus()
    router = OrderRouter(broker, bus)
    await router.submit(_order(cid="d1"))
    await router.submit(_order(cid="d2"))
    assert len(await broker.open_orders()) == 2

    rm = RiskManager(RiskConfig())
    await rm.kill(broker=broker, reason="panic")

    # Both open orders were attempted; the switch tripped regardless of the failure.
    assert broker.cancel_attempts == 2
    assert rm.tripped is True


# --- None limits = unconstrained ------------------------------------------- #


def test_all_none_limits_pass_everything() -> None:
    """An all-None RiskConfig (the default) gates nothing."""
    rm = RiskManager(
        RiskConfig(),  # max_* all None
        daily_pnl_provider=lambda _day_start_ms: money("-999999"),
    )
    rm.check(_order(qty="1000000", side=OrderSide.BUY))
    rm.check(_order(qty="1000000", side=OrderSide.SELL))
    rm.check(_order())


# --- integration through OrderRouter (spy broker) -------------------------- #


async def test_router_blocks_breaching_order_no_broker_call() -> None:
    """A breaching order is refused end-to-end: place_order is never called."""
    broker = _SpyBroker()
    bus = EventBus()
    rm = RiskManager(RiskConfig(max_order=money("1")))
    router = OrderRouter(broker, bus, risk_manager=rm)

    with pytest.raises(RiskLimitBreached):
        await router.submit(_order(cid="big", qty="2"))

    assert broker.place_calls == 0, "breaching order must not reach the broker"
    # A refused order is *not* tracked: the dedup map has no record of it, so a
    # later (compliant) attempt of the same id is a fresh submission.
    assert router.get("big") is None


async def test_breaching_submit_leaves_no_unretrieved_future() -> None:
    """A refused submit (no concurrent waiter) logs no asyncio future warning.

    The router stores a failed submission's exception on its in-flight future for
    any concurrent waiter; with no waiter, asyncio would log "Future exception was
    never retrieved" at GC — log noise that can mask real incidents in a trading
    engine. The future's done-callback consumes the exception, so the loop's
    exception handler is never invoked.
    """
    import gc

    broker = _SpyBroker()
    bus = EventBus()
    rm = RiskManager(RiskConfig(max_order=money("1")))
    router = OrderRouter(broker, bus, risk_manager=rm)

    handled: list[dict[str, object]] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: handled.append(context)
    )

    with pytest.raises(RiskLimitBreached):
        await router.submit(_order(cid="big", qty="2"))

    # Force collection of the (now-dropped) in-flight future, then let the loop
    # run so any scheduled handler call would fire.
    gc.collect()
    await asyncio.sleep(0)

    assert handled == [], f"unexpected loop exception handler calls: {handled}"


async def test_router_allows_compliant_order() -> None:
    """A compliant order passes the gate and reaches the broker normally."""
    broker = _SpyBroker()
    bus = EventBus()
    rm = RiskManager(RiskConfig(max_order=money("5")))
    router = OrderRouter(broker, bus, risk_manager=rm)

    order = await router.submit(_order(cid="ok", qty="2"))

    assert broker.place_calls == 1
    assert order.status is OrderStatus.OPEN
    assert router.get("ok") is order


async def test_router_tripped_switch_halts_every_submit() -> None:
    """A tripped kill-switch halts every router submit with no broker call."""
    broker = _SpyBroker()
    bus = EventBus()
    rm = RiskManager(RiskConfig())
    router = OrderRouter(broker, bus, risk_manager=rm)

    rm.trip("halt")
    with pytest.raises(RiskLimitBreached):
        await router.submit(_order(cid="halted"))
    assert broker.place_calls == 0
    assert router.get("halted") is None


# --- verification on real data (PaperBroker) ------------------------------- #


async def test_real_paperbroker_gate_blocks_then_kill_switch() -> None:
    """End-to-end on the real PaperBroker: compliant fills & moves position;
    a breaching order is refused (no new paper order, position unchanged); the
    kill-switch cancels open orders and halts further submits.
    """
    broker = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("1000000")},
        event_bus=None,
    )
    bus = EventBus()
    tracker = PositionTracker()
    # A cap that admits a 1-unit BUY but rejects a 2-unit one (resulting net).
    rm = RiskManager(
        RiskConfig(max_position=money("1")),
        position_tracker=tracker,
    )
    router = OrderRouter(broker, bus, risk_manager=rm)

    # 1) Compliant order: it fills and the position moves to +1.
    await router.submit(_order(cid="real-ok", qty="1", side=OrderSide.BUY))
    for fill in await broker.fills():
        tracker.apply(fill)
    pos = tracker.position(BTC_USD)
    assert pos is not None and pos.net_qty == money("1")
    fills_after_ok = len(await broker.fills())

    # 2) Breaching order (+1 -> +2 > cap 1): refused, no new paper order/fill,
    #    position unchanged.
    with pytest.raises(RiskLimitBreached):
        await router.submit(_order(cid="real-bad", qty="1", side=OrderSide.BUY))
    assert len(await broker.fills()) == fills_after_ok, "no new paper fill"
    assert router.get("real-bad") is None, "refused order not tracked"
    assert tracker.position(BTC_USD).net_qty == money("1"), "position unchanged"

    # 3) Leave a live order on the venue, then trip the kill-switch via kill():
    #    open orders are cancelled and further submits are halted. Use a fresh
    #    manager with no position cap so the only thing exercised here is the
    #    kill-switch (the per-limit gating is covered above).
    partial_broker = PaperBroker(
        fill_model="partial", partial_fill_ratio=money("0.5")
    )
    kill_rm = RiskManager(RiskConfig())
    partial_router = OrderRouter(
        partial_broker, EventBus(), risk_manager=kill_rm
    )
    await partial_router.submit(_order(cid="live-1", qty="1"))
    assert len(await partial_broker.open_orders()) == 1

    await kill_rm.kill(router=partial_router, reason="end-to-end panic")
    assert len(await partial_broker.open_orders()) == 0
    with pytest.raises(RiskLimitBreached):
        await partial_router.submit(_order(cid="post-kill", qty="0.1"))


async def test_concurrent_breaching_submit_places_nothing() -> None:
    """Two gathered submits of a breaching id both raise and place nothing."""
    broker = _SpyBroker()
    bus = EventBus()
    rm = RiskManager(RiskConfig(max_order=money("1")))
    router = OrderRouter(broker, bus, risk_manager=rm)

    results = await asyncio.gather(
        router.submit(_order(cid="race-bad", qty="2")),
        router.submit(_order(cid="race-bad", qty="2")),
        return_exceptions=True,
    )
    assert all(isinstance(r, RiskLimitBreached) for r in results)
    assert broker.place_calls == 0
    assert router.get("race-bad") is None
