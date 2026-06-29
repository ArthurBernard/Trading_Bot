"""The :class:`PortfolioRunner` — the multi-asset analogue of the runner.

Where the single-instrument :class:`~trading_bot.application.strategy_runner.
StrategyRunner` drives **one** instrument (feed → signal → ``delta_to`` → order →
router), the :class:`PortfolioRunner` drives a **whole universe** at once. On each
(daily) rebalance tick it evaluates the
:data:`~trading_bot.application.portfolio.PortfolioSignalFn` for the entire book,
turns the returned weight *vector* into per-coin target quantities, computes each
coin's signed change against the **shared**
:class:`~trading_bot.application.position_tracker.PositionTracker`, and routes
**N** idempotent, risk-gated orders through the **shared**
:class:`~trading_bot.application.order_router.OrderRouter` — then holds to the
next tick:

    feed → signal_fn → weights_to_signals → per-coin delta_to(position)
        → order → router → broker → FillEvent → tracker → (next tick's position)

It owns no money logic of its own — sizing is delegated to the pure
:func:`~trading_bot.application.portfolio.weights_to_signals`, each per-coin diff
to :meth:`~trading_bot.domain.signal.Signal.delta_to`, every submission to the
router, and every fill fold to the tracker. The runner only *sequences* those
collaborators across a universe.

Universe-complete, never partial (carried into the ADR)
-------------------------------------------------------
The book must cover the **whole universe** every tick. A coin the signal *omits*
this tick (or maps to ``0``) is **not** left untouched — it is targeted **flat**
(a full close of whatever the shared tracker reports). So the runner iterates
:attr:`PortfolioStrategy.universe`, not the weight vector's keys: a 0-weight
signal is synthesised for any omitted coin, and its ``delta_to`` the current
position closes it out. This is what makes a rebalance a *re-allocation of the
whole book*, not an additive set of new bets.

Per-coin idempotency (carried into the ADR)
-------------------------------------------
Each leg's ``client_order_id`` is ``f"{strategy.name}-{symbol}-{step}"`` —
**namespaced by symbol** so the N legs of one rebalance never collide, and
deterministic in ``step`` so a re-run / retry of the *same* rebalance dedups
**per coin** at the router (one id → one venue order). The runner stamps this id
onto whatever the ``order_factory`` returns, exactly as
:class:`StrategyRunner` does: idempotency is the runner's concern, not the
factory's.

Maker-LIMIT legs (carried into the ADR)
---------------------------------------
The default :func:`portfolio_limit_at_close_factory` prices each leg as a LIMIT
at that coin's latest close, so the in-process :class:`~trading_bot.brokers.
paper.PaperBroker` fills self-contained (at the exact close the signal saw)
without seeded mark prices; a live broker ignores the synthetic price and fills
at the venue. This mirrors :func:`~trading_bot.application.run_app.
_limit_at_close_factory`, generalised to take a per-coin instrument + close.

Per-leg failure policy (carried into the ADR)
---------------------------------------------
A rebalance is **not** all-or-nothing. Routing one leg can fail — a
:class:`~trading_bot.domain.errors.RiskLimitBreached` (the order exceeds
``max_order`` / would breach a limit / the kill-switch is tripped), or a
:class:`~trading_bot.domain.errors.BrokerError`. **The runner continues the other
legs** and collects the failures: aborting the whole book because one coin
breached a limit would leave the book in a *worse*, half-rebalanced-then-frozen
state and let one bad name veto every good one. Each failure is recorded as a
:class:`RebalanceFailure` (symbol + the exception) and surfaced on the
:class:`RebalanceResult`; a :class:`~trading_bot.application.events.LogEvent` is
emitted per failure when a bus is wired. The kill-switch is *not* a special case
here: a tripped switch simply makes **every** leg raise
:class:`RiskLimitBreached`, so the result reports N failures and zero
submissions — the halt is total, by the gate, without the runner needing to know
about it. (A caller that wants strict all-or-nothing can inspect
:attr:`RebalanceResult.failures` and act.)

Cooperative stop & cadence (carried into the ADR)
-------------------------------------------------
:meth:`run` mirrors :class:`StrategyRunner.run`: it iterates the feed, checks an
optional :class:`asyncio.Event` ``stop_event`` **at the top of each iteration**
(between rebalances, never mid-rebalance, so no leg is torn in half), and returns
the total number of orders submitted. The daily cadence is the feed's — the
runner does not busy-wait; it holds between ticks by simply awaiting the next
window the (daily) feed yields.

This module lives in the application layer: it imports the pure domain and the
sibling use-cases, holds money as :class:`~decimal.Decimal` end to end, and
performs no I/O of its own (the router/broker/feed do).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from trading_bot.application.events import EventBus, LogEvent
from trading_bot.application.portfolio import weights_to_signals
from trading_bot.domain.errors import BrokerError, RiskLimitBreached
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.domain.position import Position

if TYPE_CHECKING:
    import polars as pl

    from trading_bot.application.order_router import OrderRouter
    from trading_bot.application.portfolio import PortfolioStrategy
    from trading_bot.application.position_tracker import PositionTracker

__all__ = [
    "PortfolioRunner",
    "PortfolioOrderFactory",
    "RebalanceFailure",
    "RebalanceResult",
    "portfolio_limit_at_close_factory",
]

logger = logging.getLogger(__name__)

#: A caller-supplied per-coin order builder:
#: ``(strategy, instrument, delta, close) -> Order``. Given the coin's signed
#: target delta (``> 0`` buy, ``< 0`` sell) and its latest close, it returns the
#: :class:`Order` to route for that leg (e.g. a maker LIMIT at the close). The
#: runner overrides the order's ``client_order_id`` with its deterministic,
#: symbol-namespaced per-step id, so a factory need not (and should not rely on)
#: set one.
PortfolioOrderFactory = Callable[
    ["PortfolioStrategy", Instrument, Money, Money], Order
]

_ZERO: Money = money("0")


@dataclass(frozen=True, slots=True)
class RebalanceFailure:
    """One coin's leg that failed to route during a rebalance.

    Attributes
    ----------
    symbol : Symbol
        The coin whose leg failed.
    error : Exception
        The exception raised routing it — a
        :class:`~trading_bot.domain.errors.RiskLimitBreached` (a breached limit
        or a tripped kill-switch) or a
        :class:`~trading_bot.domain.errors.BrokerError`. Carried so a caller can
        inspect the cause without re-running.

    """

    symbol: Symbol
    error: Exception


@dataclass(frozen=True, slots=True)
class RebalanceResult:
    """The outcome of one rebalance tick — what routed and what failed.

    Attributes
    ----------
    submitted : int
        Number of legs whose order routed successfully this tick (non-zero-delta
        coins that were not refused). On-target coins (``delta == 0``) submit
        nothing and are **not** counted.
    failures : list of RebalanceFailure
        One entry per coin whose leg raised (a risk breach or a broker error),
        in universe order. Empty on a clean rebalance. See the module docstring's
        per-leg failure policy: a failure does **not** abort the other legs.

    """

    submitted: int = 0
    failures: list[RebalanceFailure] = field(default_factory=list)

    @property
    def failed(self) -> int:
        """Number of legs that failed to route this tick."""
        return len(self.failures)


class PortfolioRunner:
    """Drive a :class:`PortfolioStrategy` over a portfolio feed, routing N legs.

    On each rebalance tick the runner evaluates the strategy's weight-vector
    signal for the whole book, sizes it into per-coin target-quantity signals,
    diffs each against the shared :class:`PositionTracker`, and routes one
    idempotent, risk-gated order per non-on-target coin through the shared
    :class:`OrderRouter`. See the module docstring for the universe-complete
    rule, the per-coin idempotency scheme, the maker-LIMIT legs and the per-leg
    failure policy.

    Parameters
    ----------
    strategy : PortfolioStrategy
        The multi-asset strategy to drive: its ``universe`` (every coin the book
        must cover each tick), ``signal_fn`` (the weight vector), ``capital``
        (the base the weights are a fraction of) and ``name`` (the per-leg
        ``client_order_id`` seed) govern every rebalance.
    feed : Iterable[Mapping[Symbol, polars.DataFrame]]
        The source of causal per-coin cross-sections (e.g. a
        :class:`~trading_bot.application.portfolio_feed.PortfolioFeed`). At step
        ``t`` each coin's frame holds only bars ``≤ t`` — no lookahead. If the
        feed exposes ``asof_ms()`` it is used for the signal/Signal timestamps;
        otherwise the latest common ``time`` across the frames is derived (ns →
        ms).
    router : OrderRouter
        The shared idempotent write path. Each leg is ``await``\\ ed through
        :meth:`OrderRouter.submit`; a duplicate ``client_order_id`` (a re-run)
        is deduped there into a single venue order, and the risk gate refuses a
        breaching leg before the broker is ever touched.
    tracker : PositionTracker
        The shared live net-position read-back. ``tracker.position(instrument)``
        gives each coin's current exposure the per-coin delta is computed
        against; a ``None`` (no fill yet) is treated as flat. For the loop to
        close, the same broker's fills must reach this tracker (wire
        ``PaperBroker(event_bus=bus)`` + ``PositionTracker(event_bus=bus)``), so
        a tick's fills are reflected in the *next* tick's positions.
    event_bus : EventBus, optional
        If given, the runner emits a :class:`~trading_bot.application.events.
        LogEvent` per submitted leg and per failed leg (a human-readable trace
        of the rebalance). Defaults to ``None`` (no trace; orders still flow
        through the router's own ``OrderEvent``\\ s).
    order_factory : PortfolioOrderFactory, optional
        Builds each leg's :class:`Order` from ``(strategy, instrument, delta,
        close)``. Defaults to :func:`portfolio_limit_at_close_factory` (a maker
        LIMIT at the coin's latest close, so the paper broker fills
        self-contained). Whatever it returns, the runner overrides the
        ``client_order_id`` with its deterministic, symbol-namespaced per-step id
        (so idempotency is the runner's, not the factory's, concern).

    Examples
    --------
    >>> # runner = PortfolioRunner(strategy, feed, router, tracker, event_bus=bus)
    >>> # n_orders = await runner.run()        # drive the whole feed
    >>> # result = await runner.rebalance(frames)  # or pump one tick by hand

    """

    def __init__(
        self,
        strategy: PortfolioStrategy,
        feed: object,
        router: OrderRouter,
        tracker: PositionTracker,
        *,
        event_bus: EventBus | None = None,
        order_factory: PortfolioOrderFactory | None = None,
    ) -> None:
        self._strategy = strategy
        self._feed = feed
        self._router = router
        self._tracker = tracker
        self._bus = event_bus
        self._order_factory = (
            order_factory
            if order_factory is not None
            else portfolio_limit_at_close_factory()
        )
        # Monotonic rebalance index — also the per-leg client-order-id seed. An
        # instance counter so a fresh runner over the same feed reproduces the
        # same ids (deterministic re-run), while a single runner re-driven via
        # repeated ``run`` calls keeps advancing.
        self._step_index = 0

    @property
    def strategy(self) -> PortfolioStrategy:
        """The :class:`PortfolioStrategy` this runner drives (read-only)."""
        return self._strategy

    @property
    def step_index(self) -> int:
        """The next rebalance index (== number of ticks processed so far)."""
        return self._step_index

    async def run(
        self,
        max_steps: int | None = None,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> int:
        """Drive the feed tick-by-tick, rebalancing the whole book each tick.

        Iterates the feed (the sync iterable of causal per-coin cross-sections)
        and calls :meth:`rebalance` on each, stopping early after ``max_steps``
        ticks if given, or as soon as ``stop_event`` is set. Honours causality by
        construction — each cross-section is the feed's causal prefix and the
        runner never reads past it.

        Parameters
        ----------
        max_steps : int or None, optional
            Process at most this many rebalance ticks. ``None`` (default) drains
            the feed to exhaustion.
        stop_event : asyncio.Event or None, optional
            A cooperative stop signal, checked at the top of each iteration —
            **between** rebalances, never mid-rebalance — so an in-flight leg is
            never interrupted. ``None`` (default) runs without a stop signal.

        Returns
        -------
        int
            The total number of orders **submitted** across every rebalance this
            call (summed over ticks; on-target / refused legs are not counted).

        """
        submitted = 0
        processed = 0
        for frames in self._feed:  # type: ignore[attr-defined]
            # Cooperative stop is checked *before* the rebalance: a rebalance
            # that has begun always finishes all its legs (no leg torn
            # mid-submit); a stop only takes effect at this between-ticks
            # boundary.
            if stop_event is not None and stop_event.is_set():
                break
            if max_steps is not None and processed >= max_steps:
                break
            result = await self.rebalance(frames)
            submitted += result.submitted
            processed += 1
            # Yield to the event loop once per tick when a stop signal is in play
            # (the live/looping case), mirroring StrategyRunner — a tick that
            # submits nothing never awaits a venue, so without this a tight sync
            # loop over a live feed would starve the cooperative shutdown. This is
            # a between-ticks boundary, so it never interrupts a leg.
            if stop_event is not None:
                await asyncio.sleep(0)
        return submitted

    async def rebalance(
        self, frames: Mapping[Symbol, pl.DataFrame]
    ) -> RebalanceResult:
        """Process **one** rebalance tick: weight vector → N idempotent legs.

        Evaluates ``strategy.signal_fn(asof, frames)`` for the whole book, sizes
        the weight vector into per-coin target-quantity signals via
        :func:`~trading_bot.application.portfolio.weights_to_signals`, then for
        **every coin in the universe** (a coin the signal omitted is targeted
        flat) computes ``delta = signal.delta_to(tracker.position(instrument))``
        and, **only if ``delta != 0``**, routes one order through the router with
        the deterministic, symbol-namespaced per-step ``client_order_id``. A leg
        that raises (risk breach / broker error) is recorded and the remaining
        legs continue (see the module docstring's per-leg failure policy). The
        rebalance index is always advanced (so ids stay aligned to the tick
        sequence even on a no-trade rebalance).

        Parameters
        ----------
        frames : Mapping[Symbol, polars.DataFrame]
            The causal per-coin cross-section for this tick. The runner reads the
            latest close per coin (as exact :class:`~decimal.Decimal`) and the
            as-of timestamp from it (or from the feed's ``asof_ms``).

        Returns
        -------
        RebalanceResult
            The number of legs submitted and the list of per-coin failures (empty
            on a clean rebalance).

        """
        step = self._step_index
        # Advance the index *before* any routing so a no-trade tick still consumes
        # its slot — keeping ``f"{name}-{symbol}-{step}"`` aligned 1:1 with the
        # tick sequence (re-run determinism does not depend on the outcome).
        self._step_index += 1

        asof = self._asof_ms(frames)
        prices = self._latest_closes(frames)
        weights = self._strategy.signal_fn(asof, frames)

        # Universe-complete: cover every coin, defaulting an omitted one to a
        # 0-weight (flat) target so it is fully closed. Iterate the *universe*,
        # not the weight keys.
        full_weights: dict[Symbol, Money] = {
            symbol: weights.get(symbol, _ZERO)
            for symbol in self._strategy.universe
        }

        signals = weights_to_signals(
            full_weights,
            prices=prices,
            capital=self._strategy.capital,
            asof_ms=asof,
        )
        signal_by_symbol = {sig.instrument.symbol: sig for sig in signals}

        submitted = 0
        failures: list[RebalanceFailure] = []
        # Route in universe order for a deterministic per-tick leg sequence.
        for symbol in self._strategy.universe:
            signal = signal_by_symbol[symbol]
            instrument = signal.instrument
            current = self._tracker.position(instrument)
            position = current if current is not None else _flat(instrument)
            delta = signal.delta_to(position)
            if delta == 0:
                # Already on target (incl. a flat target against a flat position):
                # no leg.
                continue

            order = self._build_order(symbol, instrument, delta, prices[symbol], step)
            try:
                routed = await self._router.submit(order)
            except (RiskLimitBreached, BrokerError) as exc:
                # Per-leg failure: record it and continue the other legs (the
                # rebalance is not all-or-nothing — see the module docstring).
                failures.append(RebalanceFailure(symbol=symbol, error=exc))
                if self._bus is not None:
                    self._bus.emit(
                        LogEvent(
                            message=(
                                f"{self._strategy.name} step {step}: leg "
                                f"{symbol} FAILED "
                                f"({type(exc).__name__}: {exc})"
                            ),
                            level="warning",
                        )
                    )
                continue

            submitted += 1
            if self._bus is not None:
                self._bus.emit(
                    LogEvent(
                        message=(
                            f"{self._strategy.name} step {step}: "
                            f"{routed.side.value} {routed.qty} "
                            f"{routed.instrument} "
                            f"(delta={delta}, cid={routed.client_order_id})"
                        )
                    )
                )

        return RebalanceResult(submitted=submitted, failures=failures)

    def _build_order(
        self,
        symbol: Symbol,
        instrument: Instrument,
        delta: Money,
        close: Money,
        step: int,
    ) -> Order:
        """Build a leg's order, stamping its deterministic per-coin per-step id.

        Delegates the order *shape* to the ``order_factory`` (the maker-LIMIT
        default), then overrides its ``client_order_id`` with
        ``f"{strategy.name}-{symbol}-{step}"`` — symbol-namespaced so the N legs
        of one tick never collide and a re-run dedups per coin at the router.
        """
        order = self._order_factory(self._strategy, instrument, delta, close)
        # The runner owns idempotency, not the factory: stamp the per-coin,
        # per-step id regardless of what the factory chose.
        order.client_order_id = f"{self._strategy.name}-{symbol}-{step}"
        return order

    def _asof_ms(self, frames: Mapping[Symbol, pl.DataFrame]) -> int:
        """Resolve the as-of timestamp (ms) for this tick.

        Prefers the feed's ``asof_ms()`` when it exposes one (a
        :class:`~trading_bot.application.portfolio_feed.PortfolioFeed` reports the
        latest common date's close in ms); otherwise derives it from the frames'
        latest common ``time`` (dccd stamps bars in nanoseconds, so it is
        converted ns → ms). Both paths read the *latest* bar across the
        cross-section, never a future one.
        """
        feed_asof = getattr(self._feed, "asof_ms", None)
        if callable(feed_asof):
            value = feed_asof()
            if value is not None:
                return int(value)
        return self._derive_asof_ms(frames)

    @staticmethod
    def _derive_asof_ms(frames: Mapping[Symbol, pl.DataFrame]) -> int:
        """Derive the as-of ms from the frames' latest common bar time (ns → ms).

        Each coin's frame is a causal window oldest→newest; the cross-section's
        as-of is the *minimum* of the per-coin latest ``time`` (the last day on
        which **every** coin has a bar — never beyond any coin's data). dccd
        timestamps are nanoseconds, so the value is integer-divided to ms.
        """
        latest_per_coin = [
            int(frame["time"][-1]) for frame in frames.values() if frame.height > 0
        ]
        if not latest_per_coin:
            return 0
        return min(latest_per_coin) // 1_000_000

    @staticmethod
    def _latest_closes(
        frames: Mapping[Symbol, pl.DataFrame]
    ) -> dict[Symbol, Money]:
        """Read each coin's latest close as exact :class:`~decimal.Decimal`.

        Reads the last ``c`` per coin via ``money(str(...))`` — never ``float`` —
        so the sizing arithmetic in
        :func:`~trading_bot.application.portfolio.weights_to_signals` and the
        maker-LIMIT leg price stay exact.
        """
        return {
            symbol: money(str(frame["c"][-1]))
            for symbol, frame in frames.items()
            if frame.height > 0
        }


def portfolio_limit_at_close_factory(
    close_col: str = "c",
) -> PortfolioOrderFactory:
    """Build a per-coin order factory that prices each leg at its latest close.

    The portfolio analogue of :func:`~trading_bot.application.run_app.
    _limit_at_close_factory`, generalised to take the coin's instrument + close
    directly (the runner has already read the latest close as exact
    :class:`~decimal.Decimal`). Each leg is a maker LIMIT at that close, so the
    in-process :class:`~trading_bot.brokers.paper.PaperBroker` fills it
    self-contained (at the exact close the signal saw) without seeded mark
    prices; a live broker ignores the synthetic price and fills at the venue. The
    runner overrides the ``client_order_id`` afterwards.

    Parameters
    ----------
    close_col : str, optional
        Unused — kept for parity with the single-instrument factory's signature
        (the runner reads the close and passes it in, so the column name is no
        longer needed here). Defaults to ``"c"``.

    Returns
    -------
    PortfolioOrderFactory
        A ``(strategy, instrument, delta, close) -> Order`` builder producing a
        maker LIMIT for ``abs(delta)`` on the side implied by ``delta``'s sign.

    """

    def _factory(
        strategy: PortfolioStrategy,
        instrument: Instrument,
        delta: Money,
        close: Money,
    ) -> Order:
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        return Order(
            client_order_id="pending",  # overridden by the runner
            instrument=instrument,
            side=side,
            qty=abs(delta),
            type=OrderType.LIMIT,
            limit_price=close,
        )

    return _factory


def _flat(instrument: Instrument) -> Position:
    """A zero (flat) :class:`Position` for ``instrument`` — the no-fill default.

    Used when the shared tracker has no position for a coin yet, so
    :meth:`~trading_bot.domain.signal.Signal.delta_to` can be called uniformly
    (the target *is* the delta against a flat book).
    """
    return Position(
        instrument=instrument,
        net_qty=_ZERO,
        avg_entry_price=None,
        realised_pnl=_ZERO,
        fees_paid=_ZERO,
    )
