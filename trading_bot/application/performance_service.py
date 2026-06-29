"""The :class:`PerformanceService` — live PnL/KPI over the fill stream.

The service is the **read-side performance view** of execution: it observes the
venue's confirmed fills (the PnL source of truth) and reports realised PnL, fees
paid, an equity curve and the KPI ratios (Sharpe, Sortino, max drawdown, Calmar).
It is purely observational — it *never* places, amends or cancels an order. Where
the :class:`~trading_bot.application.position_tracker.PositionTracker` answers
"what do we hold?", the performance service answers "how have we *done*?".

Fill ingestion — the boundary
-----------------------------
Fills reach the service the same two ways as the tracker, both landing in
:meth:`apply`:

* **Subscribed** — constructed with an
  :class:`~trading_bot.application.events.EventBus`, the service subscribes to
  :class:`~trading_bot.application.events.FillEvent` and ``apply``\\ s each one
  automatically (other event types are ignored). The
  :class:`~trading_bot.brokers.paper.PaperBroker` (and, later, a live broker's
  private fill stream) emits ``FillEvent``\\ s, so the order -> fill ->
  performance flow is wired end to end with no polling.
* **Explicit** — a caller (e.g. a reconciliation pass that drains
  :meth:`~trading_bot.brokers.base.Broker.fills`) feeds each fill to
  :meth:`apply` directly. No bus required.

Aggregation model (carried into the ADR)
----------------------------------------
The service keeps **two** views of the same fill stream:

* a **global ordered fill list** — every fill in arrival order, across all
  instruments. This drives the *aggregate* realised-PnL / equity series.
* a **per-instrument ordered fill list** — for :meth:`position`, computed via
  :meth:`~trading_bot.domain.position.Position.from_fills` exactly like the
  :class:`PositionTracker`.

Realised PnL is **additive across instruments and across fills** (each fill
contributes an independent close-PnL term and an independent fee term — see the
:class:`~trading_bot.domain.position.Position` sign convention), so the *total*
realised PnL after the k-th global fill is the sum over instruments of
``Position.from_fills(fills_of_that_instrument_seen_through_k).realised_pnl``.
The service therefore builds its equity series **step-by-step from realised PnL**
rather than from a mark-to-market price path: the equity after the k-th fill is

    equity_k = v0 + total_realised_pnl_through_k

This is the natural curve for a fill-driven view — it moves only when a close
locks PnL in (or a fee is charged), needs no external mark price series, and
reconciles exactly to :meth:`~trading_bot.domain.position.Position.from_fills`
summed across instruments. (The pure
:func:`trading_bot.domain.performance.equity_curve` is a *single-instrument
mark-to-market* curve that needs an aligned price series; it is not used here
because the aggregate fill stream spans instruments and we have no per-step mark
price. We reuse the same *value = v0 + cumulative-PnL* shape it defines.)

Short-series KPI policy (carried into the ADR)
----------------------------------------------
The KPI ratios are statistical estimators over a *returns* path. fynance needs at
least two equity points (one return) to produce a meaningful number; with fewer,
the estimator is undefined (and fynance would divide by a zero / empty variance).
The service therefore **guards before delegating**: if the equity curve has fewer
than two points (i.e. 0 or 1 fills, so 0 returns), every KPI method returns
``0.0`` rather than raising. With two or more points the call is delegated to
:mod:`trading_bot.domain.performance` (fynance-backed) and its result returned
verbatim.

Money stays :class:`~decimal.Decimal` through the PnL core (realised PnL, fees,
equity curve); only the KPI series crosses to ``float`` at the
:func:`trading_bot.domain.performance.equity_array` boundary, exactly as the
domain layer already does.

The module is part of the application layer: it imports the pure domain and the
event bus, holds money as :class:`~decimal.Decimal` end to end, and is
deterministic in fill order.
"""

from __future__ import annotations

from trading_bot.application.events import Event, EventBus, FillEvent
from trading_bot.domain import performance as perf
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money, money
from trading_bot.domain.position import Position

__all__ = ["PerformanceService"]

_ZERO: Money = money("0")


class PerformanceService:
    """Live performance view (PnL / equity / KPIs) folded from confirmed fills.

    Construct it bare (and call :meth:`apply` for each fill) or with an
    :class:`~trading_bot.application.events.EventBus` (then it subscribes to
    :class:`~trading_bot.application.events.FillEvent` and applies fills
    automatically). Read realised PnL and fees with :meth:`realised_pnl` /
    :meth:`fees_paid`, the per-instrument exposure with :meth:`position`, the
    account-value path with :meth:`equity_curve`, and the risk ratios with
    :meth:`sharpe` / :meth:`sortino` / :meth:`max_drawdown` / :meth:`calmar`.

    Read-side only: the service observes fills and reports performance; it never
    submits, amends or cancels an order.

    Parameters
    ----------
    v0 : Money, optional
        Initial account capital, anchoring the equity curve
        (``equity = v0 + cumulative realised PnL``). Defaults to ``money("0")``,
        so the curve is the bare cumulative realised PnL.
    event_bus : EventBus, optional
        If given, the service subscribes to it and applies every
        :class:`~trading_bot.application.events.FillEvent` as it is emitted. With
        ``None`` (the default) the service is driven only by explicit
        :meth:`apply` calls.

    Examples
    --------
    >>> from trading_bot.domain.fill import Fill
    >>> from trading_bot.domain.instrument import Instrument, Symbol
    >>> from trading_bot.domain.money import money
    >>> from trading_bot.domain.order import OrderSide
    >>> inst = Instrument(Symbol("BTC", "USD"))
    >>> svc = PerformanceService(v0=money("1000"))
    >>> svc.apply(
    ...     Fill("T1", "cid-1", inst, OrderSide.BUY, money("2"), money("30000"),
    ...          money("6"), 1)
    ... )
    >>> svc.fees_paid()
    Decimal('6')
    >>> svc.realised_pnl()
    Decimal('-6')

    """

    def __init__(
        self, *, v0: Money = _ZERO, event_bus: EventBus | None = None
    ) -> None:
        self._v0: Money = v0
        # Running net position per instrument, advanced one fill at a time via
        # Position.with_fill (O(1) per fill — no full-history refold).
        self._positions: dict[Instrument, Position] = {}
        # Running aggregate realised PnL after each global fill (one entry per
        # fill). Rebuilt incrementally so equity_curve() is O(1) to read.
        self._equity: list[Money] = []
        # Running totals, kept incrementally (each fill's contribution to total
        # realised PnL is the delta of its instrument's position realised PnL).
        self._realised_pnl: Money = _ZERO
        self._fees_paid: Money = _ZERO
        # Fill ids already folded — guards against a venue re-emitting the same
        # execution (e.g. a private-WS snapshot replay after a reconnect), which
        # would otherwise silently corrupt the running realised PnL. See
        # :meth:`apply`.
        self._seen_fill_ids: set[str] = set()
        self._bus = event_bus
        if event_bus is not None:
            event_bus.subscribe(self._on_event)

    def _on_event(self, event: Event) -> None:
        """Bus handler: apply the fill of a :class:`FillEvent`, ignore the rest.

        Subscribed to the :class:`~trading_bot.application.events.EventBus`, which
        fans out every event type; the service only cares about
        :class:`~trading_bot.application.events.FillEvent`.
        """
        if isinstance(event, FillEvent):
            self.apply(event.fill)

    def apply(self, fill: Fill) -> None:
        """Fold ``fill`` into the aggregate and per-instrument performance views.

        Advances that instrument's **running**
        :class:`~trading_bot.domain.position.Position` by exactly this fill via
        :meth:`~trading_bot.domain.position.Position.with_fill` (O(1) per fill, no
        full-history refold), and updates the running aggregate realised PnL / fees
        / equity by the **delta** the fill introduced to its instrument's realised
        PnL (so totals stay the sum across instruments, and the equity series gains
        exactly one point).

        **Idempotent by ``fill_id``.** A fill whose ``fill_id`` was already folded
        is ignored (no PnL/fee/equity change), so a venue re-emitting the same
        execution (e.g. a private-WS snapshot replay after a reconnect) never
        corrupts the running realised PnL. Mirrors the
        :class:`~trading_bot.application.position_tracker.PositionTracker`.

        Parameters
        ----------
        fill : Fill
            A broker-confirmed execution (the PnL source of truth).

        """
        if fill.fill_id in self._seen_fill_ids:
            return  # duplicate execution — never double-count.
        self._seen_fill_ids.add(fill.fill_id)

        instrument = fill.instrument
        prev = self._positions.get(instrument) or Position.flat(instrument)
        now = prev.with_fill(fill)
        self._positions[instrument] = now

        # The fill's contribution to the aggregate is the delta of its instrument's
        # realised PnL / fees (one new equity point: v0 + total realised PnL).
        self._realised_pnl += now.realised_pnl - prev.realised_pnl
        self._fees_paid += now.fees_paid - prev.fees_paid

        # One new equity point: v0 + total realised PnL through this fill.
        self._equity.append(self._v0 + self._realised_pnl)

    def realised_pnl(self) -> Money:
        """Aggregate realised PnL across all instruments, **net of fees**.

        Consistent with :meth:`~trading_bot.domain.position.Position.from_fills`:
        equals the sum over instruments of that instrument's
        ``Position.from_fills(...).realised_pnl``.

        Returns
        -------
        Money
            The total realised PnL (exact :class:`~decimal.Decimal`). Zero when
            no fill has been applied.

        """
        return self._realised_pnl

    def fees_paid(self) -> Money:
        """Aggregate fees paid across all instruments and all fills.

        Equals the sum over instruments of
        ``Position.from_fills(...).fees_paid``.

        Returns
        -------
        Money
            The total fees paid (exact :class:`~decimal.Decimal`). Zero when no
            fill has been applied.

        """
        return self._fees_paid

    def position(self, instrument: Instrument) -> Position | None:
        """Return the net :class:`Position` for ``instrument``, or ``None``.

        The running per-instrument position, advanced one fill at a time (it
        equals :meth:`~trading_bot.domain.position.Position.from_fills` over every
        fill seen for ``instrument``).

        Parameters
        ----------
        instrument : Instrument
            The instrument to read.

        Returns
        -------
        Position or None
            The folded position, or ``None`` if no fill for ``instrument`` has
            been applied yet.

        """
        return self._positions.get(instrument)

    def equity_curve(self) -> tuple[Money, ...]:
        """The account-value path: ``v0`` + cumulative realised PnL per fill.

        One point per applied fill, in arrival order, each equal to ``v0`` plus
        the aggregate realised PnL through that fill (see the module docstring for
        why a realised-PnL step series is the natural fill-driven curve). Empty
        until the first fill is applied.

        Returns
        -------
        tuple of Money
            The equity at each fill step (exact :class:`~decimal.Decimal`).

        """
        return tuple(self._equity)

    # --- KPI ratios — delegate to domain.performance (fynance-backed) -------- #

    def sharpe(self, *, rf: float = 0.0, period: int = 252, log: bool = False) -> float:
        """Annualised Sharpe ratio of the equity curve.

        Delegates to :func:`trading_bot.domain.performance.sharpe`. Returns
        ``0.0`` when the equity curve has fewer than two points (no return to
        measure) — see the short-series policy in the module docstring.

        Parameters
        ----------
        rf : float, optional
            Annualised risk-free rate. Default ``0``.
        period : int, optional
            Periods per year for annualisation. Default ``252``.
        log : bool, optional
            Use log-returns instead of simple returns. Default ``False``.

        Returns
        -------
        float
            The Sharpe ratio, or ``0.0`` for a too-short series.

        """
        if len(self._equity) < 2:
            return 0.0
        return perf.sharpe(self._equity, rf=rf, period=period, log=log)

    def sortino(
        self, *, rf: float = 0.0, period: int = 252, log: bool = False
    ) -> float:
        """Annualised Sortino ratio of the equity curve.

        Delegates to :func:`trading_bot.domain.performance.sortino`. Returns
        ``0.0`` for a series with fewer than two points (short-series policy).

        Parameters
        ----------
        rf : float, optional
            Annualised risk-free rate. Default ``0``.
        period : int, optional
            Periods per year. Default ``252``.
        log : bool, optional
            Use log-returns. Default ``False``.

        Returns
        -------
        float
            The Sortino ratio, or ``0.0`` for a too-short series.

        """
        if len(self._equity) < 2:
            return 0.0
        return perf.sortino(self._equity, rf=rf, period=period, log=log)

    def max_drawdown(self, *, raw: bool = False) -> float:
        """Maximum drawdown of the equity curve.

        Delegates to :func:`trading_bot.domain.performance.max_drawdown`. Returns
        ``0.0`` for a series with fewer than two points (short-series policy).

        Parameters
        ----------
        raw : bool, optional
            If ``True`` return the absolute decline; otherwise (default) the
            fractional drawdown.

        Returns
        -------
        float
            The maximum drawdown, or ``0.0`` for a too-short series.

        """
        if len(self._equity) < 2:
            return 0.0
        return perf.max_drawdown(self._equity, raw=raw)

    def calmar(self, *, period: int = 252) -> float:
        """Calmar ratio of the equity curve.

        Delegates to :func:`trading_bot.domain.performance.calmar`. Returns
        ``0.0`` for a series with fewer than two points (short-series policy).

        Parameters
        ----------
        period : int, optional
            Periods per year. Default ``252``.

        Returns
        -------
        float
            The Calmar ratio, or ``0.0`` for a too-short series.

        """
        if len(self._equity) < 2:
            return 0.0
        return perf.calmar(self._equity, period=period)
