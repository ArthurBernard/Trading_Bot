"""The :class:`StrategyRunner` — the engine's live loop, data → orders.

The runner is the **conductor** of an execution run: it pulls causal bar windows
from a :class:`~trading_bot.application.data_feed.DataFeed`, evaluates the
:class:`~trading_bot.application.strategy.Strategy`'s signal on each, turns that
venue-neutral :class:`~trading_bot.domain.signal.Signal` into a *target position
change* against the live :class:`~trading_bot.application.position_tracker.
PositionTracker`, and routes the resulting :class:`~trading_bot.domain.order.
Order` through the idempotent :class:`~trading_bot.application.order_router.
OrderRouter`. It is the modern replacement for the legacy ``StrategyBot``
iterator, and the last piece that closes the order ↔ fill loop:

    feed → strategy.evaluate → signal.delta_to(position) → order → router
       → broker → FillEvent → tracker → (next step's position)

It owns no money logic of its own — every quantity decision is delegated to the
pure domain (:meth:`Signal.delta_to`), every submission to the router, every fill
fold to the tracker. The runner only *sequences* those collaborators.

The loop shape (carried into the ADR)
-------------------------------------
The feed is a **synchronous** ``Iterable`` of causal windows, but the router is
``async`` (a venue I/O boundary). So the runner is an ``async`` driver that
iterates the sync feed and ``await``\\ s one submission per step. The public
surface is deliberately small:

* :meth:`run` — drive the whole feed (optionally capped at ``max_steps``,
  optionally observing a cooperative ``stop_event``), returning the number of
  orders actually submitted;
* :meth:`step` — process **one** already-pulled window, the unit :meth:`run`
  calls in a loop. Exposed so a caller (a live driver, a test) can pump windows
  in by hand and keep the step index it owns.

Cooperative stop (carried into the ADR)
---------------------------------------
:meth:`run` accepts an optional :class:`asyncio.Event` ``stop_event``. The loop
checks it **at the top of each iteration** — *before* pulling/processing the next
window — and exits cleanly when it is set. The check is *between* steps, never
mid-``step``: a step that has begun (and may have ``await``\\ ed a submission)
always runs to completion, so a cooperative stop can never tear an order in half.
This is the runner's half of the :class:`~trading_bot.application.orchestrator.
Orchestrator`'s graceful shutdown — the orchestrator owns one shared event, sets
it once, and every runner drains to its next between-steps boundary.

Because each window is the feed's causal prefix ``frame[: t + 1]`` and the runner
never reads beyond the window handed to it, **causality is preserved by
construction** — the runner cannot peek ahead even if it wanted to.

Per-step idempotency (carried into the ADR)
-------------------------------------------
Each step's order carries a **deterministic** ``client_order_id`` of the form
``f"{strategy.name}-{step_index}"``. The step index is monotonic across a run
(and across :meth:`run` re-invocations on the *same* runner instance), so
re-running the *same sequence* on a *fresh* runner produces the *same* ids — and
the router, which dedups on ``client_order_id``, turns a re-run into a no-op at
the venue (no duplicate broker order). This is the runner's half of the E4
idempotency contract: the router guarantees "one id → one venue order", and the
runner guarantees "one step → one stable id".

Warmup (carried into the ADR)
-----------------------------
Warmup is **not** re-implemented here. :meth:`Strategy.evaluate` already returns a
*flat* (zero-exposure) signal until ``lookback`` bars are present. A flat target
against a flat position yields ``delta == 0`` → no order. So no order is ever
submitted during warmup, and the runner needs no special-case: the warmup
guarantee lives in one place (the strategy), and the runner inherits it.

This module lives in the application layer: it imports the pure domain and the
sibling use-cases, holds money as :class:`~decimal.Decimal` end to end, and
performs no I/O of its own (the router/broker do).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from trading_bot.application.events import EventBus, LogEvent
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.domain.position import Position

if TYPE_CHECKING:
    import polars as pl

    from trading_bot.application.data_feed import DataFeed
    from trading_bot.application.order_router import OrderRouter
    from trading_bot.application.position_tracker import PositionTracker
    from trading_bot.application.strategy import Strategy

__all__ = ["StrategyRunner", "OrderFactory"]

#: A caller-supplied order builder: ``(strategy, delta, bars) -> Order``. Given
#: the signed target delta (``> 0`` buy, ``< 0`` sell) and the current bar
#: window, it returns the :class:`Order` to submit (e.g. a LIMIT priced off the
#: window's close instead of the default MARKET). The runner overrides the
#: order's ``client_order_id`` with its deterministic per-step id, so a factory
#: need not (and should not rely on) set one.
OrderFactory = Callable[["Strategy", Money, "pl.DataFrame"], Order]

_ZERO: Money = money("0")


class StrategyRunner:
    """Drive a :class:`Strategy` over a :class:`DataFeed`, routing the deltas.

    On each causal bar window the runner evaluates the strategy's signal, reads
    the live position from the tracker, computes the signed target change with
    :meth:`Signal.delta_to`, and — when that change is non-zero — submits one
    :class:`Order` through the router. See the module docstring for the loop
    shape, the per-step idempotency scheme and the warmup guarantee.

    Parameters
    ----------
    strategy : Strategy
        The strategy to drive: its ``instrument``, ``signal_fn``,
        ``reference_qty`` (the scale for a fractional-exposure signal) and
        ``lookback`` (warmup) govern every step. ``strategy.name`` seeds the
        per-step ``client_order_id``.
    feed : DataFeed
        The source of causal bar windows (``frame[: t + 1]`` at step ``t``). The
        runner iterates it; at step ``t`` it sees only bars ``≤ t`` — no
        lookahead.
    router : OrderRouter
        The idempotent write path. Each step's order is ``await``\\ ed through
        :meth:`OrderRouter.submit`; a duplicate ``client_order_id`` (a re-run)
        is deduped there into a single venue order.
    tracker : PositionTracker
        The live net-position read-back. ``tracker.position(instrument)`` gives
        the current exposure the delta is computed against; a ``None`` (no fill
        yet) is treated as flat. For the loop to close, the same broker's fills
        must reach this tracker (wire ``PaperBroker(event_bus=bus)`` +
        ``PositionTracker(event_bus=bus)``), so a step's fills are reflected in
        the *next* step's position.
    event_bus : EventBus, optional
        If given, the runner emits a :class:`~trading_bot.application.events.
        LogEvent` per submitted order (a human-readable trace of the run).
        Defaults to ``None`` (no trace emitted; orders still flow through the
        router's own ``OrderEvent``\\ s).
    order_factory : OrderFactory, optional
        Builds the :class:`Order` for a step from ``(strategy, delta, bars)``.
        Defaults to a MARKET order for ``abs(delta)`` on the side of ``delta``.
        Whatever it returns, the runner overrides the ``client_order_id`` with
        its deterministic per-step id (so idempotency is the runner's, not the
        factory's, concern).

    Examples
    --------
    >>> # runner = StrategyRunner(strategy, feed, router, tracker, event_bus=bus)
    >>> # n_orders = await runner.run()          # drive the whole feed
    >>> # await runner.step(some_window)         # or pump one window by hand

    """

    def __init__(
        self,
        strategy: Strategy,
        feed: DataFeed,
        router: OrderRouter,
        tracker: PositionTracker,
        *,
        event_bus: EventBus | None = None,
        order_factory: OrderFactory | None = None,
    ) -> None:
        self._strategy = strategy
        self._feed = feed
        self._router = router
        self._tracker = tracker
        self._bus = event_bus
        self._order_factory = order_factory
        # Monotonic step index — also the per-step client-order-id seed. It is an
        # instance counter so a fresh runner over the same feed reproduces the
        # same ids (deterministic re-run), while a *single* runner re-driven via
        # repeated ``run`` calls keeps advancing (never reusing an id within one
        # instance's lifetime).
        self._step_index = 0

    @property
    def strategy(self) -> Strategy:
        """The :class:`Strategy` this runner drives (read-only)."""
        return self._strategy

    @property
    def step_index(self) -> int:
        """The next step index (== number of windows processed so far)."""
        return self._step_index

    async def run(
        self,
        max_steps: int | None = None,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> int:
        """Drive the feed window-by-window, submitting the per-step deltas.

        Iterates the feed (the sync iterable of causal windows) and calls
        :meth:`step` on each, stopping early after ``max_steps`` windows if
        given, or as soon as ``stop_event`` is set. Honours causality by
        construction — each window is the feed's causal prefix and the runner
        never reads past it.

        Parameters
        ----------
        max_steps : int or None, optional
            Process at most this many windows (bounds an otherwise feed-length
            run; useful for tests and live feeds). ``None`` (default) drains the
            feed to exhaustion.
        stop_event : asyncio.Event or None, optional
            A cooperative stop signal. When given, the loop checks it at the top
            of each iteration — **between** steps, never mid-``step`` — and
            returns cleanly the moment it is set, so an in-flight submission is
            never interrupted. ``None`` (default) runs without a stop signal.
            This is the hook the :class:`~trading_bot.application.orchestrator.
            Orchestrator` uses for graceful shutdown.

        Returns
        -------
        int
            The number of orders **submitted** during this call (steps where the
            delta was non-zero). Warmup and on-target steps submit nothing and
            are not counted.

        """
        submitted = 0
        processed = 0
        for bars in self._feed:
            # Check the cooperative stop *before* processing this window: a step
            # that has begun always finishes (no order torn mid-submit); a stop
            # only takes effect at this between-steps boundary.
            if stop_event is not None and stop_event.is_set():
                break
            if max_steps is not None and processed >= max_steps:
                break
            order = await self.step(bars)
            if order is not None:
                submitted += 1
            processed += 1
            # When a stop signal is in play (the orchestrator's looping/live
            # case), yield control to the event loop once per iteration. A step
            # that submits nothing (warmup / on-target) never ``await``s a venue,
            # so without this a tight sync loop over a live feed would starve the
            # loop — and the cooperative ``shutdown`` coroutine could never run.
            # This is a between-steps boundary, so it never interrupts a submit.
            if stop_event is not None:
                await asyncio.sleep(0)
        return submitted

    async def step(self, bars: pl.DataFrame) -> Order | None:
        """Process **one** causal window: evaluate, diff, and maybe submit.

        Evaluates ``strategy.evaluate(bars)`` (flat during warmup), reads the
        current position from the tracker, computes
        ``delta = signal.delta_to(position, reference_qty=strategy.reference_qty)``
        and, **only if ``delta != 0``**, builds an order (MARKET by default, or
        via the ``order_factory``) with the deterministic per-step
        ``client_order_id`` and submits it through the router. The step index is
        always advanced (so ids stay aligned to the bar sequence even on a
        no-order step).

        Parameters
        ----------
        bars : polars.DataFrame
            The causal bar window for this step (``frame[: t + 1]``). The runner
            reads only this window — never a later bar.

        Returns
        -------
        Order or None
            The submitted order (the router's tracked instance), or ``None`` when
            the step submitted nothing (warmup, or already on target /
            ``delta == 0``).

        """
        step = self._step_index
        # Advance the index *before* any early return so a no-order step still
        # consumes its slot — keeping ``f"{name}-{step}"`` aligned 1:1 with the
        # bar sequence (re-run determinism does not depend on order outcomes).
        self._step_index += 1

        signal = self._strategy.evaluate(bars)
        current = self._tracker.position(self._strategy.instrument)
        net_qty = current.net_qty if current is not None else _ZERO
        # delta_to also handles a flat current position; we pass net_qty via a
        # cheap flat Position only when the tracker has none, to keep the call
        # uniform without reaching into the signal's internals.
        position = (
            current
            if current is not None
            else Position(
                instrument=self._strategy.instrument,
                net_qty=net_qty,
                avg_entry_price=None,
                realised_pnl=_ZERO,
                fees_paid=_ZERO,
            )
        )
        delta = signal.delta_to(
            position, reference_qty=self._strategy.reference_qty
        )

        if delta == 0:
            # Already on target (incl. flat-during-warmup → flat position): no
            # order. This is where the warmup guarantee lands — see module doc.
            return None

        order = self._build_order(delta, bars, step)
        submitted = await self._router.submit(order)
        if self._bus is not None:
            self._bus.emit(
                LogEvent(
                    message=(
                        f"{self._strategy.name} step {step}: "
                        f"{submitted.side.value} {submitted.qty} "
                        f"{submitted.instrument} "
                        f"(delta={delta}, cid={submitted.client_order_id})"
                    )
                )
            )
        return submitted

    def _build_order(self, delta: Money, bars: pl.DataFrame, step: int) -> Order:
        """Build the step's order, stamping the deterministic per-step id.

        Uses the ``order_factory`` if given (then overrides its
        ``client_order_id``), otherwise a MARKET order for ``abs(delta)`` on the
        side implied by the sign of ``delta``. The id is always
        ``f"{strategy.name}-{step}"`` so a re-run dedups at the router.
        """
        cid = f"{self._strategy.name}-{step}"
        if self._order_factory is not None:
            order = self._order_factory(self._strategy, delta, bars)
            # The runner owns idempotency, not the factory: stamp the per-step id
            # regardless of what the factory chose.
            order.client_order_id = cid
            return order
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        return Order(
            client_order_id=cid,
            instrument=self._strategy.instrument,
            side=side,
            qty=abs(delta),
            type=OrderType.MARKET,
        )
