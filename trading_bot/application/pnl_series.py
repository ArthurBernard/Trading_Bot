"""Pure PnL-series helpers — the realised-PnL / equity curve from a fill fold.

The data foundation for the dashboard's PnL chart. Given a strategy's
**confirmed fills** (the PnL source of truth), these pure helpers derive a
**per-mode realised-PnL / equity curve over time** by folding the fills in
timestamp order:

    equity(t) = v0 + Σ realised_pnl(fills ≤ t)

exactly the *value = v0 + cumulative-realised-PnL* shape the
:class:`~trading_bot.application.performance_service.PerformanceService` uses for
its fill-driven equity curve — so a strategy's derived curve reconciles to its
running engine's ``perf.realised_pnl()`` to the cent. Realised PnL is computed
via the domain :class:`~trading_bot.domain.position.Position` fold (a running
per-instrument :meth:`~trading_bot.domain.position.Position.with_fill`), never a
re-derivation, so the two can never diverge.

Two helpers, both pure (no I/O, money exact :class:`~decimal.Decimal`):

* :func:`equity_series` — fold an ordered fill list into a list of
  :class:`EquityPoint` ``(ts_ms, realised_pnl, equity)``, one point per fill.
* :func:`by_mode` — split a list of :class:`~trading_bot.storage.sqlite_store.
  StoredFill` (fill + its ``mode`` tag) into ``{mode: [Fill, ...]}`` so live and
  testnet (fake money — never combined) become separate series.

Why a per-mode split (carried into the ADR)
-------------------------------------------
Testnet is **fake money**: a testnet fill's PnL must never be added to a live
(real-money) curve, and vice versa. The mode is a **storage / deployment** tag
(it lives on the :class:`~trading_bot.storage.sqlite_store.StoredFill` row, never
on the pure domain :class:`~trading_bot.domain.fill.Fill`), and :func:`by_mode`
is where the fill stream is partitioned before each partition is folded into its
own curve from the same ``v0``.

The module is part of the application layer but holds no state and does no I/O:
it consumes domain objects and returns value objects, money as
:class:`~decimal.Decimal` end to end, deterministic in fill order.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from trading_bot.domain.money import Money, money
from trading_bot.domain.position import Position

if TYPE_CHECKING:
    from trading_bot.domain.fill import Fill
    from trading_bot.storage.sqlite_store import StoredFill

__all__ = ["EquityPoint", "equity_series", "by_mode"]

_ZERO: Money = money("0")


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One point of a realised-PnL / equity curve — ``(ts_ms, realised_pnl, equity)``.

    The value object :func:`equity_series` yields per fill: the fill's timestamp,
    the **aggregate realised PnL** (net of fees, summed across every instrument)
    through that fill, and the account ``equity`` (``v0`` + that realised PnL).
    Money is exact :class:`~decimal.Decimal`.

    Attributes
    ----------
    ts_ms : int
        The fill's execution timestamp, milliseconds since the Unix epoch (UTC).
    realised_pnl : Money
        Cumulative realised PnL (net of fees) through this fill, across all
        instruments in the folded stream.
    equity : Money
        The account value at this fill: ``v0 + realised_pnl``.

    """

    ts_ms: int
    realised_pnl: Money
    equity: Money


def equity_series(
    fills: Iterable[Fill], *, v0: Money = _ZERO
) -> list[EquityPoint]:
    """Fold ``fills`` (in timestamp order) into an equity curve — one point per fill.

    Sorts the fills by their ``ts`` (stably, so same-timestamp fills keep their
    input order — the execution tie-break), then folds them: a running
    :class:`~trading_bot.domain.position.Position` per instrument is advanced by
    each fill via :meth:`~trading_bot.domain.position.Position.with_fill`, and the
    fill's contribution to the aggregate realised PnL is the **delta** it
    introduced to its instrument's realised PnL — exactly how the
    :class:`~trading_bot.application.performance_service.PerformanceService` builds
    its curve, so the two agree to the cent. Each point's ``equity`` is ``v0`` plus
    the aggregate realised PnL through that fill.

    Parameters
    ----------
    fills : Iterable[Fill]
        The confirmed fills to fold (any instrument mix). Ordered by ``ts``
        internally; the caller need not pre-sort.
    v0 : Money, optional
        The account's starting capital, anchoring the curve
        (``equity = v0 + cumulative realised PnL``). Defaults to ``money("0")``,
        so the curve is the bare cumulative realised PnL.

    Returns
    -------
    list of EquityPoint
        One point per fill, in ascending timestamp order. Empty for no fills.

    """
    # Sort by timestamp; Python's sort is stable, so same-ts fills keep input
    # order (the execution tie-break the domain Fill documents).
    ordered = sorted(fills, key=lambda f: f.ts)
    positions: dict[object, Position] = {}
    realised: Money = _ZERO
    points: list[EquityPoint] = []
    for fill in ordered:
        instrument = fill.instrument
        prev = positions.get(instrument) or Position.flat(instrument)
        now = prev.with_fill(fill)
        positions[instrument] = now
        # The fill's contribution to the aggregate is the delta of its
        # instrument's realised PnL (fees included — with_fill nets them).
        realised += now.realised_pnl - prev.realised_pnl
        points.append(
            EquityPoint(ts_ms=fill.ts, realised_pnl=realised, equity=v0 + realised)
        )
    return points


def by_mode(stored: Iterable[StoredFill]) -> dict[str, list[Fill]]:
    """Split tagged fills into ``{mode: [Fill, ...]}`` — one bucket per deployment mode.

    Partitions a :class:`~trading_bot.storage.sqlite_store.StoredFill` stream by
    its ``mode`` tag (``"paper"`` / ``"testnet"`` / ``"live"``), returning the
    plain domain :class:`~trading_bot.domain.fill.Fill` in each bucket in input
    order. This is the seam that keeps live and testnet (fake money) as **separate
    series** — each bucket is folded into its own curve from the same ``v0`` (see
    :func:`equity_series`). First-seen mode order is preserved so the view is
    deterministic.

    Parameters
    ----------
    stored : Iterable[StoredFill]
        The tagged fills read from a :class:`~trading_bot.storage.sqlite_store.
        SqliteStore` (:meth:`~trading_bot.storage.sqlite_store.SqliteStore.
        stored_fills`).

    Returns
    -------
    dict[str, list[Fill]]
        A mapping of deployment mode to its fills, first-seen mode order.

    """
    buckets: dict[str, list[Fill]] = {}
    for record in stored:
        buckets.setdefault(record.mode, []).append(record.fill)
    return buckets
