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

Venue-minimum order preparation (carried into the ADR)
-------------------------------------------------------
When an :class:`~trading_bot.application.instrument_specs.InstrumentSpecResolver`
is injected (with the unit's ``exchange``), every non-zero leg passes through the
pure :func:`~trading_bot.application.order_prep.prepare_leg` policy before an
order is built: the leg's ``|delta|`` is lot-quantized against the **resolved**
venue spec and compared to the binding minimum (``min_qty`` /
``min_notional / price``) — submitted when at/above it, rounded **up** to it when
within ``min_order_ratio`` of it, **skipped** (no submit, one info
:class:`~trading_bot.application.events.LogEvent`) when further below; a sell
reducing a long is capped at the held quantity. A degraded resolver (a venue
metadata fetch failed — leaf 01's seam) emits ONE warning ``LogEvent`` per runner
lifetime and the legs fall back to today's permissive path. See
:mod:`~trading_bot.application.order_prep` for the pinned policy (and for the
note that the single-instrument :class:`StrategyRunner` is a follow-up seam).

The resolved spec shapes the **order values only** — deliberately. The
:class:`~trading_bot.application.position_tracker.PositionTracker` buckets
positions by the full frozen :class:`~trading_bot.domain.instrument.Instrument`,
and every fill population keys the **bare** ``Instrument(symbol)``: the store
rebuilds instruments symbol-only on replay, the live adapters build
``Instrument(symbol)`` on their fills, and the
:class:`~trading_bot.application.risk.RiskManager` reads exposure by the
*order's* instrument. Stamping the metadata-rich resolved instrument onto the
:class:`~trading_bot.domain.order.Order` would therefore split a restored book
into two tracker buckets (bare vs resolved) and blind the ``max_position`` gate —
so the leg's order keeps the bare instrument, and the venue spec is applied
upstream to its *quantity* here.

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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from trading_bot.application.events import EventBus, LogEvent
from trading_bot.application.order_prep import prepare_leg
from trading_bot.application.portfolio import weights_to_signals
from trading_bot.domain.errors import BrokerError, RiskLimitBreached
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.domain.position import Position

if TYPE_CHECKING:
    import polars as pl

    from trading_bot.application.instrument_specs import InstrumentSpecResolver
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
PortfolioOrderFactory = Callable[["PortfolioStrategy", Instrument, Money, Money], Order]

_ZERO: Money = money("0")


def _now_ms() -> int:
    """Wall-clock time in milliseconds since the Unix epoch (UTC).

    Used only for :attr:`PortfolioRunner.last_eval_ms` bookkeeping — a
    diagnostic, never consulted by the freshness gate itself.
    """
    return int(time.time() * 1000)


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
        ``t`` each coin's frame holds only bars ``≤ t`` — no lookahead. The
        as-of timestamp stamped on the signal/Signal is always derived from the
        frames' latest common ``time`` (ns → ms) — a pure computation over data
        already read, never a second fetch. If the feed additionally exposes
        ``tail_asof_ms()`` (a cheap, bounded probe — see
        :meth:`~trading_bot.application.portfolio_feed.PortfolioFeed
        .tail_asof_ms`), :meth:`rebalance_latest` uses it as an idle-tick
        freshness gate before paying for a full ``latest()`` read.
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
    capital_provider : Callable[[], Money] or None, optional
        A **lazy** capital-base provider read on **every** :meth:`rebalance`. When
        given, the tick sizes the weight vector against ``provider()`` instead of
        the strategy's static ``capital`` — so a deposit / withdrawal / policy
        flip is hot (it takes effect on the next rebalance with **no** runner
        rebuild). The provider is called **exactly once per rebalance**, giving
        the whole book one consistent base for the tick; **this single call — not
        a lock — is the concurrency guarantee** that every leg of a tick sizes
        against the same base even if a deposit lands mid-tick. ``None`` (default)
        keeps the legacy static ``strategy.capital``, byte-for-byte unchanged.
    spec_resolver : InstrumentSpecResolver or None, optional
        The per-unit venue-spec resolver (see
        :class:`~trading_bot.application.instrument_specs.InstrumentSpecResolver`
        — cached per ``(exchange, symbol)`` for the process lifetime). When
        given, every non-zero leg is prepared through the venue-minimum policy
        (:func:`~trading_bot.application.order_prep.prepare_leg`) before its
        order is built — see the module docstring. ``None`` (default) keeps the
        legacy unprepared path, byte-for-byte unchanged. Requires ``exchange``.
    exchange : str or None, optional
        The venue key the unit trades on (the portfolio config's ``venue`` —
        the same string the supervisor tags the unit's store with), passed to
        ``spec_resolver.resolve``. Required when ``spec_resolver`` is given;
        ignored (and defaulted to ``None``) otherwise.
    min_order_ratio : Decimal, optional
        The round-up threshold of the venue-minimum policy as a fraction of
        the binding minimum, in ``(0, 1]`` (see
        :class:`~trading_bot.application.config.PortfolioStrategyConfig`).
        Defaults to ``0.5``. Only consulted when ``spec_resolver`` is given.

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
        capital_provider: Callable[[], Money] | None = None,
        spec_resolver: InstrumentSpecResolver | None = None,
        exchange: str | None = None,
        min_order_ratio: Money = money("0.5"),
    ) -> None:
        if spec_resolver is not None and (exchange is None or not exchange.strip()):
            raise ValueError(
                "PortfolioRunner needs the unit's exchange to resolve venue "
                "specs: pass exchange= alongside spec_resolver="
            )
        if not (0 < min_order_ratio <= 1):
            # Fail at construction, not mid-rebalance: a bad ratio inside the
            # leg loop would abort the whole tick (it is outside the per-leg
            # try/except by design — a config error is not a leg failure).
            raise ValueError(
                f"min_order_ratio must be in (0, 1], got {min_order_ratio}"
            )
        self._strategy = strategy
        self._feed = feed
        self._router = router
        self._tracker = tracker
        self._bus = event_bus
        self._capital_provider = capital_provider
        self._spec_resolver = spec_resolver
        self._exchange = exchange
        self._min_order_ratio = min_order_ratio
        # One degraded-resolver warning per runner (= unit) lifetime — see the
        # module docstring's venue-minimum section.
        self._spec_degraded_warned = False
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
        # The as-of (ms) of the last **completed** rebalance evaluation (the full
        # path) — ``None`` before the first tick ever runs. The idle-tick
        # freshness gate in `rebalance_latest` compares a cheap tail probe
        # against this to decide whether a new common date might exist before
        # paying for the full `feed.latest()` load. Only `rebalance_latest`
        # updates it (a plain `rebalance`/`run` backtest drive is unaffected).
        self._last_asof_ms: int | None = None
        # Wall-clock (ms since epoch) of the last time `rebalance_latest` was
        # *attempted* — set at the top of every call, whether it skips via the
        # gate or runs the full evaluation. Not consulted by the gate itself;
        # surfaced for a follow-up dashboard PR ("unit last checked at ...") so
        # an idle-but-alive unit is distinguishable from a stalled one.
        self._last_eval_ms: int | None = None

    @property
    def strategy(self) -> PortfolioStrategy:
        """The :class:`PortfolioStrategy` this runner drives (read-only)."""
        return self._strategy

    @property
    def step_index(self) -> int:
        """The next rebalance index (== number of ticks processed so far)."""
        return self._step_index

    @property
    def last_asof_ms(self) -> int | None:
        """The as-of (ms) of the last **completed** rebalance, or ``None`` before the first."""
        return self._last_asof_ms

    @property
    def last_eval_ms(self) -> int | None:
        """Wall-clock ms of the last `rebalance_latest` attempt, or ``None`` before the first."""
        return self._last_eval_ms

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

    async def rebalance(self, frames: Mapping[Symbol, pl.DataFrame]) -> RebalanceResult:
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
            latest close per coin (as exact :class:`~decimal.Decimal`) and derives
            the as-of timestamp from these same frames (see
            :meth:`_derive_asof_ms`) — never a fresh read.

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

        asof = self._derive_asof_ms(frames)
        prices = self._latest_closes(frames)
        weights = self._strategy.signal_fn(asof, frames)

        # Universe-complete: cover every coin, defaulting an omitted one to a
        # 0-weight (flat) target so it is fully closed. Iterate the *universe*,
        # not the weight keys.
        full_weights: dict[Symbol, Money] = {
            symbol: weights.get(symbol, _ZERO) for symbol in self._strategy.universe
        }

        # Call the lazy capital provider exactly once per rebalance so the whole
        # book sizes against one consistent base for this tick (a deposit landing
        # mid-tick affects the *next* rebalance, never a half of this one) — this
        # single call, not a lock, is the per-tick concurrency guarantee. With no
        # provider the legacy static ``strategy.capital`` is used unchanged.
        capital = (
            self._capital_provider()
            if self._capital_provider is not None
            else self._strategy.capital
        )
        signals = weights_to_signals(
            full_weights,
            prices=prices,
            capital=capital,
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

            if self._spec_resolver is not None:
                # Venue-minimum order preparation (see the module docstring):
                # resolve the venue spec (cached after the first tick), run the
                # pure policy, and act on its decision. The order below still
                # carries the BARE instrument — the resolved spec shapes the
                # quantity only, never the tracker/risk keying.
                delta = await self._prepare_delta(
                    symbol, delta, position.net_qty, prices[symbol], step
                )
                if delta == 0:
                    # The policy skipped the leg (dust / capped below the
                    # minimum / quantized to zero); already logged.
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

    async def rebalance_latest(self) -> RebalanceResult | None:
        """Rebalance the book over the feed's **latest** cross-section — for a daemon.

        Reads the feed's most recent causal cross-section and runs **one**
        :meth:`rebalance` over it. Where :meth:`run` *drains* the feed once
        (replay/backtest), this is the single, on-demand rebalance a
        **scheduler-driven daemon** calls each tick: per-coin deltas are computed
        against the live tracker, so a tick over unchanged weights/data submits
        nothing and only changed targets trade. Idempotent under repetition.

        Bounded per tick. A live :class:`~trading_bot.application.portfolio_feed.
        PortfolioFeed` exposes :meth:`~trading_bot.application.portfolio_feed.
        PortfolioFeed.latest`, which returns the **full aligned** cross-section
        (every common date, oldest→newest) with a **single** store read — this is
        used when present (mirroring :meth:`~trading_bot.application.strategy_runner
        .StrategyRunner.step_latest`). That full window is *still causal*: it is the
        exact final window a full drain would yield (the last growing prefix is the
        whole aligned frame), so there is no lookahead. A feed that exposes no
        ``latest()`` (a plain iterable, a backtest/test fake) falls back to
        draining and keeping the last yielded cross-section, which is identically
        the final causal window.

        Idle-tick freshness gate
        ------------------------
        A daily-bar strategy polled every 60s has, at best, **one** new common
        date per **1440** ticks — the other 1439 would call :meth:`latest`
        (reads every coin's full history and re-aligns the cross-section) only
        to conclude "nothing new". Before paying for that, a cheap tail probe
        (:meth:`~trading_bot.application.portfolio_feed.PortfolioFeed
        .tail_asof_ms`, when the feed exposes one) checks whether the universe's
        latest common date might have advanced past :attr:`last_asof_ms`. When it
        is certain there is none, the tick returns ``None`` **without** touching
        :meth:`latest` or the ``signal_fn`` at all. Any doubt — no prior baseline
        yet (the first-ever tick), the feed offers no tail probe, an empty tail
        read, or the probe raising — falls through to the full path: the gate is
        an optimisation only, never a correctness dependency.

        Off the event loop
        -------------------
        When the full path does run, the heavy sync work (:meth:`latest`'s
        per-coin history read + inner-join alignment, and the tail probe itself)
        is offloaded via :func:`asyncio.to_thread`, so a real rebalance never
        stalls concurrent event-loop traffic (e.g. the dashboard's
        ``/api/health``). This is safe *for this unit*: each managed unit owns
        its own engine (feed/tracker/router are never shared across units — see
        :class:`~trading_bot.application.supervisor.StrategySupervisor`), and the
        only caller of `rebalance_latest` is the daemon's scheduled tick, which
        never re-enters the **same** unit concurrently (the daemon's APScheduler
        job runs with its default ``max_instances=1``, and
        :meth:`~trading_bot.application.supervisor.StrategySupervisor.step_all`
        awaits each unit's step sequentially) — so no two threads ever touch this
        instance's feed/tracker at once.

        Returns
        -------
        RebalanceResult or None
            The tick's result; ``None`` if the freshness gate skipped this tick,
            or if the feed currently yields no closed cross-section (e.g. the
            universe has no common closed bar yet).

        """
        self._last_eval_ms = _now_ms()
        feed_latest = getattr(self._feed, "latest", None)
        if callable(feed_latest):
            if await self._should_skip_tick():
                return None
            # Heavy sync work off the loop — see the "Off the event loop" note
            # above for why this is safe against concurrent access.
            latest: Mapping[Symbol, pl.DataFrame] | None = await asyncio.to_thread(
                feed_latest
            )
            if not latest or not any(f.height > 0 for f in latest.values()):
                return None
            result = await self.rebalance(latest)
            self._last_asof_ms = self._derive_asof_ms(latest)
            return result
        # Fallback: a plain iterable feed (no ``latest()``) — the backtest/test
        # fake path, never the live daemon's `PortfolioFeed`. No cheap probe
        # exists for it (draining is the only way to find the last window), so
        # the gate does not apply here; behaviour is unchanged from before.
        latest = None
        for frames in self._feed:  # type: ignore[attr-defined]
            latest = frames
        if latest is None:
            return None
        result = await self.rebalance(latest)
        self._last_asof_ms = self._derive_asof_ms(latest)
        return result

    async def _should_skip_tick(self) -> bool:
        """The idle-tick freshness gate: True only when certain nothing is new.

        Conservative by construction — any doubt falls through to ``False``
        (evaluate the full path): no prior baseline yet, the feed exposes no
        ``tail_asof_ms`` probe, the probe finds no bar in its tail window, or the
        probe raises. Only a **determinate** tail read that is not newer than
        :attr:`last_asof_ms` causes a skip. The probe itself runs via
        :func:`asyncio.to_thread` (it is still a dccd read, just a bounded one).
        """
        if self._last_asof_ms is None:
            return False  # nothing to compare against yet (the first-ever tick)
        tail_asof_ms = getattr(self._feed, "tail_asof_ms", None)
        if not callable(tail_asof_ms):
            return False  # the feed offers no cheap probe (e.g. a test fake)
        try:
            # Anchor the probe on our own last-known as-of (never wall-clock —
            # see DccdFeed.tail_asof_ms's docstring for why), so a stale store
            # never permanently defeats the gate.
            tail_asof = await asyncio.to_thread(
                tail_asof_ms, since_ms=self._last_asof_ms
            )
        except Exception:
            logger.debug(
                "%s: freshness-gate tail probe raised; falling through to a "
                "full evaluation (the gate is an optimisation, never a "
                "correctness dependency)",
                self._strategy.name,
                exc_info=True,
            )
            return False
        if tail_asof is None:
            return False
        if tail_asof <= self._last_asof_ms:
            logger.debug(
                "%s: freshness gate skipped this tick (tail asof %s <= last asof %s)",
                self._strategy.name,
                tail_asof,
                self._last_asof_ms,
            )
            return True
        return False

    async def _prepare_delta(
        self,
        symbol: Symbol,
        delta: Money,
        position_qty: Money,
        close: Money,
        step: int,
    ) -> Money:
        """Run one leg through the venue-minimum policy; return the final delta.

        Resolves the venue spec for ``(exchange, symbol)`` (cached by the
        resolver after the first tick), applies
        :func:`~trading_bot.application.order_prep.prepare_leg` to the signed
        ``delta`` and acts on the decision:

        * ``skip`` → emits one info :class:`LogEvent` with the policy's reason
          and returns ``0`` (the caller routes nothing — the residual is
          recomputed naturally on the next rebalance);
        * ``round_up`` → emits one info :class:`LogEvent` and returns the
          bumped quantity on the original delta's side;
        * ``submit`` → returns the (lot-quantized, possibly sell-capped)
          quantity on the original delta's side.

        A degraded resolver (a venue metadata fetch failed and fell back to a
        bare instrument) additionally emits ONE warning :class:`LogEvent` per
        runner lifetime; the leg itself degrades to the permissive path via the
        bare spec. Never raises on venue-metadata trouble — the resolver
        swallows fetch failures by contract, so a leg can never abort the
        rebalance from here.
        """
        assert self._spec_resolver is not None and self._exchange is not None
        spec = await self._spec_resolver.resolve(self._exchange, symbol)
        if (
            not self._spec_degraded_warned
            and self._exchange.lower() in self._spec_resolver.degraded
        ):
            self._spec_degraded_warned = True
            if self._bus is not None:
                self._bus.emit(
                    LogEvent(
                        message=(
                            f"{self._strategy.name}: venue spec fetch for "
                            f"{self._exchange} is degraded — orders are "
                            f"prepared permissively (no venue minimums) until "
                            f"the daemon restarts"
                        ),
                        level="warning",
                    )
                )
        decision = prepare_leg(
            delta,
            spec,
            position_qty=position_qty,
            min_order_ratio=self._min_order_ratio,
            price=close,
        )
        if decision.action == "skip":
            if self._bus is not None:
                self._bus.emit(
                    LogEvent(
                        message=(
                            f"{self._strategy.name} step {step}: leg {symbol} "
                            f"skipped — {decision.reason}"
                        ),
                        level="info",
                    )
                )
            return _ZERO
        if decision.action == "round_up" and self._bus is not None:
            self._bus.emit(
                LogEvent(
                    message=(
                        f"{self._strategy.name} step {step}: leg {symbol} "
                        f"rounded up to the venue minimum — {decision.reason}"
                    ),
                    level="info",
                )
            )
        # The policy returns the final ABSOLUTE quantity; it rides the original
        # delta's side (the policy never flips a leg's direction).
        return decision.qty if delta > 0 else -decision.qty

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
        built = self._order_factory(self._strategy, instrument, delta, close)
        # The runner owns idempotency, not the factory: build the final Order
        # with the deterministic, symbol-namespaced per-step id set *at
        # construction* (via dataclasses.replace, which re-runs validation)
        # rather than mutating the client_order_id afterwards — the id is the
        # aggregate's identity and must not change once the Order exists.
        return replace(built, client_order_id=f"{self._strategy.name}-{symbol}-{step}")

    @staticmethod
    def _derive_asof_ms(frames: Mapping[Symbol, pl.DataFrame]) -> int:
        """Derive the as-of ms from the frames' latest common bar time (ns → ms).

        Each coin's frame is a causal window oldest→newest; the cross-section's
        as-of is the *minimum* of the per-coin latest ``time`` (the last day on
        which **every** coin has a bar — never beyond any coin's data). dccd
        timestamps are nanoseconds, so the value is integer-divided to ms.

        A pure computation over ``frames`` already in hand — **never** a fresh
        read. An earlier version of this resolution preferred a feed's own
        ``asof_ms()`` (e.g. :meth:`~trading_bot.application.portfolio_feed
        .PortfolioFeed.asof_ms`) when available, but that method performs its
        own full ``_read_all()`` — calling it here, after ``frames`` had already
        been loaded (by :meth:`rebalance_latest` or a backtest ``run``), read
        every coin's whole history a **second** time for no new information: the
        latest common time already in ``frames`` is exactly the same value (both
        the live and replay callers pass either the full aligned cross-section
        or a causal prefix of it). Deriving locally keeps a tick to the single
        read it already paid for.
        """
        latest_per_coin = [
            int(frame["time"][-1]) for frame in frames.values() if frame.height > 0
        ]
        if not latest_per_coin:
            return 0
        return min(latest_per_coin) // 1_000_000

    @staticmethod
    def _latest_closes(frames: Mapping[Symbol, pl.DataFrame]) -> dict[Symbol, Money]:
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
