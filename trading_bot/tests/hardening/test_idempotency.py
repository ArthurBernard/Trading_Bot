"""Hardening: idempotent submit survives retries and ambiguous failures.

The invariant under test — **order submission is idempotent**: a retry (or a
concurrent double-submit) of the same ``client_order_id`` never creates a second
venue order, and an *ambiguous* placement failure (the venue took the order but
the acknowledgement was lost) never leaves the engine silently believing it
succeeded — reconciliation adopts the venue's truth rather than the engine
re-submitting a duplicate.

Fault story
-----------
Two faults, two guarantees:

* **retry / concurrency** — submitting one id twice (sequentially and via
  ``asyncio.gather``) calls ``place_order`` **exactly once** (the router's dedup
  map + in-flight-future guard).
* **ambiguous failure** —
  :meth:`~trading_bot.tests.hardening._faulty_broker.FaultyBroker.
  ambiguous_next_place` lands the order on the venue but raises to the caller.
  The engine must **not** silently track a success: ``submit`` raises, and the
  order is recorded ``REJECTED`` locally (an honest "we do not know it is live").
  A naive retry of the same id is then deduped (no second venue order), and
  :func:`~trading_bot.application.reconcile.reconcile` converges to the venue's
  truth — **exactly one order exists for that intent** afterwards.

What the engine guarantees vs the E10-02 live policy
----------------------------------------------------
Offline, the engine guarantees: (1) no duplicate venue order from a retry of a
known id; (2) an ambiguous failure surfaces (never a silent success); (3)
reconcile is the recovery — it adopts the live venue order rather than letting a
retry double-submit. What it does **not** yet do, and what the **E10-02 live
policy** will add, is *venue-level* idempotency: a venue-side dedup token on the
order so that even a retry the *engine* forgot (e.g. across a crash before the
``REJECTED`` record persisted) cannot create a second order at the venue. Today
the dedup is purely engine-side (in-memory map); E10-02 makes the venue itself
reject the duplicate. These tests prove the engine-side half and document the gap.

All offline: :class:`~trading_bot.brokers.paper.PaperBroker` under a fault
wrapper. Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import asyncio

import pytest

from trading_bot.application import (
    EventBus,
    OrderRouter,
    PositionTracker,
    reconcile,
)
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain import (
    BrokerError,
    Instrument,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Symbol,
    money,
)
from trading_bot.tests.hardening._faulty_broker import FaultyBroker

BTC_USD = Instrument(Symbol("BTC", "USD"))


def _limit(cid: str, qty: str = "1", price: str = "30000") -> Order:
    """A realistic limit BUY order keyed by ``cid`` (the idempotency key)."""
    return Order(
        client_order_id=cid,
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money(qty),
        type=OrderType.LIMIT,
        limit_price=money(price),
    )


def _engine(
    broker: FaultyBroker,
) -> tuple[EventBus, OrderRouter, PositionTracker]:
    """Wire a fresh bus + router + tracker over ``broker``."""
    bus = EventBus()
    router = OrderRouter(broker, bus)
    tracker = PositionTracker()
    return bus, router, tracker


async def test_sequential_duplicate_submit_places_once() -> None:
    """Two sequential submits of one id place exactly one venue order.

    The steady-state dedup: the second ``submit`` of the same ``client_order_id``
    returns the already-tracked order and never re-calls the broker.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    _, router, _ = _engine(broker)

    # Half-fill so the order stays open (a single live order to count).
    inner.arm_partial(money("0.5"))
    first = await router.submit(_limit("dup-1", qty="4"))
    second = await router.submit(_limit("dup-1", qty="4"))

    assert second is first  # same tracked object, not a new submission
    assert broker.venue_place_count == 1  # exactly one venue order
    assert len(await broker.open_orders()) == 1


async def test_concurrent_duplicate_submit_places_once() -> None:
    """``asyncio.gather`` of two submits of one id still places exactly once.

    The concurrency guard: two coroutines racing on the same id interleave at the
    first ``await``; the in-flight-future map ensures only one reaches the broker
    and the other awaits its result.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    _, router, _ = _engine(broker)

    inner.arm_partial(money("0.5"))
    a, b = await asyncio.gather(
        router.submit(_limit("race-1", qty="4")),
        router.submit(_limit("race-1", qty="4")),
    )

    assert a is b  # both resolved to the same tracked order
    assert broker.venue_place_count == 1  # exactly one venue order despite the race
    assert len(await broker.open_orders()) == 1


async def test_ambiguous_failure_is_not_a_silent_success() -> None:
    """An ambiguous placement raises — the engine never silently tracks a success.

    The order lands on the venue but the response is "lost": ``submit`` must
    raise (not return a happy order), and the locally tracked order must be
    ``REJECTED`` — an honest "we do not know this is live", never a fabricated
    OPEN.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    _, router, _ = _engine(broker)

    inner.arm_partial(money("0.5"))  # leave the landed order open on the venue
    broker.ambiguous_next_place()
    with pytest.raises(BrokerError):
        await router.submit(_limit("amb-1", qty="4"))

    # The engine did NOT invent a success: the tracked order is REJECTED.
    tracked = router.get("amb-1")
    assert tracked is not None
    assert tracked.status is OrderStatus.REJECTED
    # But the venue actually HAS the order live (the ambiguity).
    venue_open = await broker.open_orders()
    assert {o.client_order_id for o in venue_open} == {"amb-1"}


async def test_retry_after_ambiguous_failure_does_not_double_submit() -> None:
    """A retry of the same id after an ambiguous failure is deduped (no 2nd order).

    The dangerous reflex is "submit failed, retry it" — which, with the order
    already live on the venue, would double-submit. The engine-side dedup map
    holds: the retry of the same id returns the recorded (REJECTED) attempt and
    never re-calls the broker, so the venue still has exactly one order.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    _, router, _ = _engine(broker)

    broker.ambiguous_next_place()
    with pytest.raises(BrokerError):
        await router.submit(_limit("amb-2", qty="4"))
    assert broker.venue_place_count == 1  # the ambiguous order landed: one venue order

    # Naive retry of the same id: deduped, no second placement.
    retried = await router.submit(_limit("amb-2", qty="4"))
    assert retried.status is OrderStatus.REJECTED  # the recorded attempt
    assert broker.venue_place_count == 1  # STILL one venue order — no duplicate


async def test_reconcile_after_ambiguous_failure_yields_one_order() -> None:
    """Reconcile is the recovery: it converges to exactly one order for the intent.

    After an ambiguous failure the venue holds the order live and the engine has
    a stale ``REJECTED`` record under the same id. ``reconcile`` reads the venue's
    truth and converges: the order is **adopted** (not ingested as a second
    object, not re-submitted), so there is **exactly one** order for that intent —
    the venue's — and the position is rebuilt from the venue's confirmed fills.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    bus, router, tracker = _engine(broker)

    # Ambiguous placement: order lands, caller sees failure.
    inner.arm_partial(money("0.5"))  # stays open on the venue
    broker.ambiguous_next_place()
    with pytest.raises(BrokerError):
        await router.submit(_limit("amb-3", qty="4"))
    assert broker.venue_place_count == 1

    result = await reconcile(broker, router, tracker, event_bus=bus)

    # The venue order shares the cid the router already holds (the REJECTED
    # record), so reconcile *adopts* it in place — never ingests a second object
    # and never re-submits.
    assert result.adopted_orders == 1
    assert result.ingested_orders == 0
    # No re-submission and no duplicate: the venue order is reconciled in place.
    assert broker.venue_place_count == 1
    # Exactly one tracked entry for the intent, matching the one venue order.
    venue_open = await broker.open_orders()
    assert {o.client_order_id for o in venue_open} == {"amb-3"}
    assert list(router.tracked_orders()) == ["amb-3"]
    # The position is rebuilt from the venue's confirmed fills (the partial fill).
    assert tracker.position(BTC_USD) is not None
    assert tracker.position(BTC_USD).net_qty == money("2")  # half of 4

    # And it is idempotent thereafter.
    again = await reconcile(broker, router, tracker, event_bus=bus)
    assert broker.venue_place_count == 1
    assert again.ingested_orders == 0
