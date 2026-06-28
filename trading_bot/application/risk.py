"""The :class:`RiskManager` — the engine's pre-trade gate + kill-switch.

This is the **last safety block before a venue ever sees an order**. Every order
the :class:`~trading_bot.application.order_router.OrderRouter` is about to submit
passes through :meth:`RiskManager.check` *before* ``broker.place_order`` is
called, so a breaching order — or any order at all once the kill-switch is
tripped — raises :class:`~trading_bot.domain.errors.RiskLimitBreached` and is
**never transmitted**. The router wires the gate in front of the broker call (see
:meth:`~trading_bot.application.order_router.OrderRouter.submit`); a raise means
no venue call and no half-tracked order.

The four checks, in order (carried into the ADR)
------------------------------------------------
:meth:`check` evaluates these in a fixed order and raises on the first breach:

1. **Kill-switch first.** If :meth:`trip` has fired, *every* order is refused
   regardless of the limits — the switch is the hard halt, so it is tested before
   any per-order arithmetic.
2. **``max_order``** — the largest size a *single* order may request. Breach when
   ``order.qty > max_order`` (``qty`` is always the positive order magnitude).
3. **``max_position``** — the largest *absolute net* position any instrument may
   hold **after** this order lands. The resulting net is the current net plus the
   order's *signed* quantity (``+qty`` for BUY, ``−qty`` for SELL); breach when
   ``abs(resulting) > max_position``. Current net comes from the injected
   :class:`~trading_bot.application.position_tracker.PositionTracker`
   (``None`` / no position seen yet ⇒ flat, ``0``). This gates the *resulting*
   exposure, not the order size, so an order that *reduces* an over-cap position
   is never blocked by it.
4. **``max_daily_loss``** — the loss (quote units) at which trading halts for the
   day. Breach when the day's realised loss **already** ``>= max_daily_loss``
   (the limit halts *new* orders once the loss is reached; it does not try to
   predict the order's own PnL — fills are the source of PnL truth, and an order
   that has not filled has realised nothing). See *Daily-loss sourcing* below.

Each limit is independent and optional: a ``None`` limit (the
:class:`~trading_bot.application.config.RiskConfig` default) is *unconstrained*
and its check is skipped. An all-``None`` config + an un-tripped switch passes
everything.

Daily-loss sourcing & reset (carried into the ADR)
--------------------------------------------------
The risk manager must not realise PnL itself (that is the
:class:`~trading_bot.application.position_tracker.PositionTracker` /
:class:`~trading_bot.application.performance_service.PerformanceService` job), so
the coupling is kept **thin**: the day's realised PnL is read through an injected
zero-arg callable, ``daily_pnl_provider: Callable[[], Money]``, returning the
**signed realised PnL for the current day** (a *loss* is negative). The manager
turns it into a loss as ``loss = -daily_pnl`` and breaches when
``loss >= max_daily_loss``. The wiring layer (the service factory) typically
passes ``lambda: perf.realised_pnl()`` for a per-day ``PerformanceService``, or
any equivalent.

For callers that do not want to wire a provider, :meth:`record_daily_pnl` is a
built-in thin setter: feed it the running daily realised PnL and the manager
reads back from its own store. When **neither** a provider nor a recorded value
is available the daily-loss check is treated as *no loss yet* (``0``) — i.e. it
never blocks on missing data; it only ever halts on an **observed** loss.

"Daily" is an explicit, caller-driven reset: :meth:`reset_day` zeroes the
recorded daily PnL (the simplest correct choice — the manager owns no clock). A
scheduler calls it at the day boundary; a provider-backed manager resets its day
by resetting the *provider's* source. There is deliberately no date-keyed magic
inside the manager: the engine already owns scheduling, and an implicit clock
inside a pure-ish gate would be a hidden, hard-to-test dependency.

The module is part of the application layer: it imports the pure domain and the
event/position primitives, holds money as :class:`~decimal.Decimal` end to end,
and performs no I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from trading_bot.brokers.base import Broker
from trading_bot.domain.errors import RiskLimitBreached
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import Order, OrderSide

if TYPE_CHECKING:
    from trading_bot.application.config import RiskConfig
    from trading_bot.application.order_router import OrderRouter
    from trading_bot.application.position_tracker import PositionTracker

__all__ = ["RiskManager"]

logger = logging.getLogger(__name__)

_ZERO: Money = money("0")

#: The synthetic ``threshold`` reported in a :class:`RiskLimitBreached` raised by
#: the kill-switch (which has no numeric threshold of its own).
_KILL_SWITCH_LIMIT = "kill_switch"


class RiskManager:
    """Pre-trade limit gate + kill-switch — refuses an order before it is placed.

    Construct it with a :class:`~trading_bot.application.config.RiskConfig` (the
    limits) and, optionally, the
    :class:`~trading_bot.application.position_tracker.PositionTracker` (for the
    ``max_position`` resulting-exposure check) and a ``daily_pnl_provider`` (for
    the ``max_daily_loss`` check). Call :meth:`check` on every order before it
    reaches the broker — the
    :class:`~trading_bot.application.order_router.OrderRouter` does this
    automatically when constructed with ``risk_manager=...``.

    Parameters
    ----------
    config : RiskConfig
        The limits to enforce (``max_order``, ``max_position``,
        ``max_daily_loss``). Any limit left ``None`` is unconstrained.
    position_tracker : PositionTracker, optional
        Source of current net exposure for the ``max_position`` check. If
        ``None``, ``max_position`` treats current exposure as flat (``0``) — so
        it then gates purely on the order's own signed quantity. Wire the tracker
        whenever ``max_position`` is set.
    daily_pnl_provider : callable, optional
        Zero-arg callable returning the **signed realised PnL for the current
        day** as :class:`~decimal.Decimal` (a loss is negative); the manager
        derives the day's loss as its negation. If ``None``, the manager reads
        the value last given to :meth:`record_daily_pnl` (default ``0`` until
        set) — so the daily-loss check only ever halts on an *observed* loss.

    Attributes
    ----------
    tripped : bool
        Whether the kill-switch is currently engaged (read-only property).

    Examples
    --------
    >>> from trading_bot.application.config import RiskConfig
    >>> from trading_bot.domain.money import money
    >>> rm = RiskManager(RiskConfig(max_order=money("1")))
    >>> rm.tripped
    False

    """

    def __init__(
        self,
        config: RiskConfig,
        *,
        position_tracker: PositionTracker | None = None,
        daily_pnl_provider: Callable[[], Money] | None = None,
    ) -> None:
        self._config = config
        self._positions = position_tracker
        self._daily_pnl_provider = daily_pnl_provider
        # The locally-recorded daily realised PnL, used when no provider is
        # injected. Reset to zero by ``reset_day``.
        self._recorded_daily_pnl: Money = _ZERO
        self._tripped = False
        self._trip_reason: str | None = None

    # --- the gate ---------------------------------------------------------- #

    def check(self, order: Order) -> None:
        """Raise :class:`RiskLimitBreached` if ``order`` may not be placed.

        The single pre-trade gate. Evaluates, in order, the kill-switch then the
        ``max_order``, ``max_position`` and ``max_daily_loss`` limits (see the
        module docstring), and raises on the **first** breach with a clear
        ``limit``/``value``/``threshold``. Returns ``None`` (silently) when every
        applicable check passes; ``None`` limits are skipped.

        This method is read-only — it never mutates ``order`` or any state — so a
        caller that catches the raise is free to leave the order untracked (the
        router does exactly that: a refused order makes no broker call and is not
        recorded as a submission).

        Parameters
        ----------
        order : Order
            The order about to be submitted.

        Raises
        ------
        RiskLimitBreached
            If the kill-switch is tripped, or any set limit would be breached.

        """
        if self._tripped:
            # The kill-switch is a hard halt: refuse before any per-order maths.
            raise RiskLimitBreached(
                _KILL_SWITCH_LIMIT,
                value=_ZERO,
                threshold=_ZERO,
            )

        self._check_max_order(order)
        self._check_max_position(order)
        self._check_max_daily_loss()

    def _check_max_order(self, order: Order) -> None:
        """Breach if the order's size exceeds ``max_order``."""
        cap = self._config.max_order
        if cap is not None and order.qty > cap:
            raise RiskLimitBreached("max_order", value=order.qty, threshold=cap)

    def _check_max_position(self, order: Order) -> None:
        """Breach if the **resulting** absolute net position exceeds ``max_position``."""
        cap = self._config.max_position
        if cap is None:
            return
        current = self._current_net(order)
        resulting = current + self._signed_qty(order)
        magnitude = abs(resulting)
        if magnitude > cap:
            raise RiskLimitBreached(
                "max_position", value=magnitude, threshold=cap
            )

    def _check_max_daily_loss(self) -> None:
        """Breach if the day's realised loss has already reached ``max_daily_loss``."""
        cap = self._config.max_daily_loss
        if cap is None:
            return
        # daily_pnl is signed (loss negative); the loss magnitude is its negation,
        # floored at zero so a profitable day never registers as a "negative loss".
        daily_pnl = self._daily_pnl()
        loss = -daily_pnl if daily_pnl < 0 else _ZERO
        if loss >= cap:
            raise RiskLimitBreached(
                "max_daily_loss", value=loss, threshold=cap
            )

    def _current_net(self, order: Order) -> Money:
        """Current signed net exposure for the order's instrument (flat if none)."""
        if self._positions is None:
            return _ZERO
        position = self._positions.position(order.instrument)
        return position.net_qty if position is not None else _ZERO

    @staticmethod
    def _signed_qty(order: Order) -> Money:
        """The order's signed quantity: ``+qty`` for BUY, ``−qty`` for SELL."""
        return order.qty if order.side is OrderSide.BUY else -order.qty

    def _daily_pnl(self) -> Money:
        """The current day's signed realised PnL (provider, else recorded)."""
        if self._daily_pnl_provider is not None:
            return self._daily_pnl_provider()
        return self._recorded_daily_pnl

    # --- daily-loss feed --------------------------------------------------- #

    def record_daily_pnl(self, daily_pnl: Money) -> None:
        """Record the running **signed** daily realised PnL (a loss is negative).

        The thin built-in feed for the ``max_daily_loss`` check, for callers that
        do not inject a ``daily_pnl_provider``. Set it to the day's realised PnL
        so far (e.g. ``perf.realised_pnl()`` for a per-day
        :class:`~trading_bot.application.performance_service.PerformanceService`);
        the manager reads it back in :meth:`check`. Ignored when a provider was
        injected (the provider is authoritative then).

        Parameters
        ----------
        daily_pnl : Decimal
            Signed realised PnL for the current day (negative = loss).

        """
        self._recorded_daily_pnl = daily_pnl

    def reset_day(self) -> None:
        """Reset the recorded daily PnL to zero — the day-boundary roll-over.

        The explicit, caller-driven "new day" hook (the manager owns no clock):
        a scheduler calls it at the day boundary so the ``max_daily_loss`` check
        starts the day fresh. Only affects the *recorded* value (:meth:`record_daily_pnl`);
        a provider-backed manager resets its day by resetting the provider's
        source.
        """
        self._recorded_daily_pnl = _ZERO

    # --- kill-switch ------------------------------------------------------- #

    @property
    def tripped(self) -> bool:
        """Whether the kill-switch is engaged (every :meth:`check` then raises)."""
        return self._tripped

    @property
    def trip_reason(self) -> str | None:
        """Why the kill-switch was tripped, or ``None`` if not tripped."""
        return self._trip_reason

    def trip(self, reason: str) -> None:
        """Engage the kill-switch: every subsequent :meth:`check` raises.

        Idempotent — re-tripping keeps the switch engaged and overwrites the
        reason. Does **not** cancel open orders by itself; use :meth:`kill` to
        both cancel and trip in one call.

        Parameters
        ----------
        reason : str
            Human-readable reason for the halt (stored on :attr:`trip_reason`).

        """
        self._tripped = True
        self._trip_reason = reason
        logger.warning("kill-switch tripped: %s", reason)

    def reset(self) -> None:
        """Clear the kill-switch, re-enabling order placement.

        Clears the tripped flag and the stored reason; the per-order limits are
        enforced as before. Does not touch the daily-loss state (use
        :meth:`reset_day` for that).
        """
        self._tripped = False
        self._trip_reason = None
        logger.warning("kill-switch reset")

    async def kill(
        self,
        router: OrderRouter | None = None,
        broker: Broker | None = None,
        *,
        reason: str = "kill-switch engaged",
    ) -> None:
        """Cancel every open order and trip the kill-switch — the hard halt.

        The documented "panic" entry point. Cancels all currently-open orders,
        then :meth:`trip`\\ s the switch so no new order can be placed. Cancellation
        runs *before* the trip so the cancels themselves are not refused by the
        gate; cancelling is a *reducing* action and is never risk-gated.

        Open orders are sourced from the ``router`` when given (its tracked
        orders, cancelled via :meth:`~trading_bot.application.order_router.
        OrderRouter.cancel` so local state transitions too); otherwise from the
        ``broker`` directly (``broker.open_orders()`` → ``broker.cancel_order``).
        Pass at least one. A cancellation that fails is logged and skipped so one
        stuck order never blocks the halt — the switch is still tripped.

        Parameters
        ----------
        router : OrderRouter, optional
            The router whose tracked orders to cancel (preferred: keeps the
            router's local state consistent). Mutually inclusive-or with
            ``broker``.
        broker : Broker, optional
            The broker to cancel directly against, when no router is available.
        reason : str, optional
            The trip reason recorded for the halt.

        Raises
        ------
        ValueError
            If neither ``router`` nor ``broker`` is given.

        """
        if router is None and broker is None:
            raise ValueError("kill() needs a router or a broker to cancel against")

        if router is not None:
            await self._cancel_via_router(router)
        elif broker is not None:
            await self._cancel_via_broker(broker)

        self.trip(reason)

    async def _cancel_via_router(self, router: OrderRouter) -> None:
        """Cancel every order the router tracks that is still live on a venue."""
        for cid, order in router.tracked_orders().items():
            # Only orders with a venue id are live on a venue; terminal/untracked
            # ones cannot (and need not) be cancelled.
            if order.venue_order_id is None or order.is_terminal:
                continue
            try:
                await router.cancel(cid)
            except Exception:
                logger.exception("kill: failed to cancel order %s", cid)

    async def _cancel_via_broker(self, broker: Broker) -> None:
        """Cancel every open order the broker reports, directly."""
        for order in await broker.open_orders():
            venue_id = order.venue_order_id
            if venue_id is None:
                continue
            try:
                await broker.cancel_order(venue_id)
            except Exception:
                logger.exception(
                    "kill: failed to cancel venue order %s", venue_id
                )
