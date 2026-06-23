"""Rich rendering helpers for the ``trading-bot`` CLI — pure, table-building.

The CLI's commands (:mod:`trading_bot.interfaces.cli.main`) stay *thin*: they
wire the engine, run it, then hand the resulting state to one of the pure helpers
here to turn into a :class:`rich.table.Table` (or a short string). Keeping the
rendering out of the command bodies means a table can be unit-tested directly
from a known state — no :class:`~typer.testing.CliRunner`, no engine — and the
commands carry no formatting logic.

Money discipline
----------------
Every monetary / quantity value is rendered with :func:`fmt_money`, which formats
the exact :class:`~decimal.Decimal` via ``str`` (optionally normalised to a fixed
number of places) and **never** routes through ``float``. The KPI *ratios*
(Sharpe, Sortino, drawdown, Calmar) are genuine floats (statistical estimators
over a returns path — see :class:`~trading_bot.application.performance_service.
PerformanceService`) and are rendered with :func:`fmt_ratio`.

These helpers are presentation-only: they read domain/application objects and
produce :mod:`rich` renderables. They perform no I/O and hold no business logic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from rich.table import Table

if TYPE_CHECKING:
    from trading_bot.application.performance_service import PerformanceService
    from trading_bot.domain.instrument import Instrument
    from trading_bot.domain.order import Order
    from trading_bot.domain.position import Position

__all__ = [
    "fmt_money",
    "fmt_ratio",
    "positions_table",
    "open_orders_table",
    "kpi_table",
]


def fmt_money(value: Decimal | None, *, places: int | None = None) -> str:
    """Format an exact :class:`~decimal.Decimal` money/qty value as a string.

    Never goes through ``float`` — the value is rendered from its exact decimal
    form. With ``places`` given, the value is quantised (display only) to that
    many decimal places via :meth:`~decimal.Decimal.quantize`; otherwise its
    natural string form is used.

    Parameters
    ----------
    value : Decimal or None
        The value to format. ``None`` renders as ``"-"`` (e.g. a flat position's
        average entry price).
    places : int or None, optional
        Fixed decimal places for display. ``None`` (default) keeps the value's
        natural form.

    Returns
    -------
    str
        The formatted value (``"-"`` for ``None``).

    """
    if value is None:
        return "-"
    if places is not None:
        quantum = Decimal(1).scaleb(-places)
        return str(value.quantize(quantum))
    return str(value)


def fmt_ratio(value: float, *, places: int = 4) -> str:
    """Format a KPI ratio (a genuine float) to ``places`` decimal places.

    Parameters
    ----------
    value : float
        The ratio (Sharpe, Sortino, drawdown, Calmar). These are statistical
        estimators over a returns path and are legitimately ``float``.
    places : int, optional
        Decimal places to show. Default ``4``.

    Returns
    -------
    str
        The formatted ratio.

    """
    return f"{value:.{places}f}"


def positions_table(
    positions: dict[Instrument, Position], *, title: str = "Positions"
) -> Table:
    """Build a :class:`rich.table.Table` of net positions, one row per instrument.

    Columns: instrument, net qty, average entry price, realised PnL, fees paid —
    every numeric column formatted via :func:`fmt_money` (exact, no float). A
    flat ``avg_entry_price`` (``None``) renders as ``"-"``. An empty mapping
    yields a header-only table.

    Parameters
    ----------
    positions : dict of Instrument to Position
        The positions to render (e.g. ``PositionTracker.all_positions()``).
    title : str, optional
        The table title. Default ``"Positions"``.

    Returns
    -------
    rich.table.Table
        The rendered table.

    """
    table = Table(title=title)
    table.add_column("Instrument")
    table.add_column("Net qty", justify="right")
    table.add_column("Avg entry", justify="right")
    table.add_column("Realised PnL", justify="right")
    table.add_column("Fees", justify="right")

    for instrument in sorted(positions, key=str):
        pos = positions[instrument]
        table.add_row(
            str(instrument),
            fmt_money(pos.net_qty),
            fmt_money(pos.avg_entry_price),
            fmt_money(pos.realised_pnl),
            fmt_money(pos.fees_paid),
        )
    return table


def open_orders_table(
    orders: list[Order], *, title: str = "Open orders"
) -> Table:
    """Build a :class:`rich.table.Table` of open orders, one row per order.

    Columns: client-order-id, venue-order-id, instrument, side, type, qty,
    filled qty, status. Quantities are formatted via :func:`fmt_money`. An empty
    list yields a header-only table.

    Parameters
    ----------
    orders : list of Order
        The open orders to render (e.g. ``await broker.open_orders()``).
    title : str, optional
        The table title. Default ``"Open orders"``.

    Returns
    -------
    rich.table.Table
        The rendered table.

    """
    table = Table(title=title)
    table.add_column("Client id")
    table.add_column("Venue id")
    table.add_column("Instrument")
    table.add_column("Side")
    table.add_column("Type")
    table.add_column("Qty", justify="right")
    table.add_column("Filled", justify="right")
    table.add_column("Status")

    for order in orders:
        table.add_row(
            order.client_order_id,
            order.venue_order_id or "-",
            str(order.instrument),
            order.side.value,
            order.type.value,
            fmt_money(order.qty),
            fmt_money(order.filled_qty),
            order.status.value,
        )
    return table


def kpi_table(perf: PerformanceService, *, title: str = "Performance (KPI)") -> Table:
    """Build a :class:`rich.table.Table` of the engine's performance KPIs.

    Two-column metric/value table over a
    :class:`~trading_bot.application.performance_service.PerformanceService`:
    realised PnL, fees paid and the equity endpoint as exact
    :class:`~decimal.Decimal` (via :func:`fmt_money`); Sharpe, Sortino, max
    drawdown and Calmar as floats (via :func:`fmt_ratio`). The equity endpoint is
    the last point of :meth:`~trading_bot.application.performance_service.
    PerformanceService.equity_curve` (``"-"`` when no fill has landed).

    Parameters
    ----------
    perf : PerformanceService
        The performance view to read (after a run, or rebuilt from stored fills).
    title : str, optional
        The table title. Default ``"Performance (KPI)"``.

    Returns
    -------
    rich.table.Table
        The rendered KPI table.

    """
    table = Table(title=title)
    table.add_column("Metric")
    table.add_column("Value", justify="right")

    curve = perf.equity_curve()
    equity_end = curve[-1] if curve else None

    table.add_row("Realised PnL", fmt_money(perf.realised_pnl()))
    table.add_row("Fees paid", fmt_money(perf.fees_paid()))
    table.add_row("Equity (end)", fmt_money(equity_end))
    table.add_row("Sharpe", fmt_ratio(perf.sharpe()))
    table.add_row("Sortino", fmt_ratio(perf.sortino()))
    table.add_row("Max drawdown", fmt_ratio(perf.max_drawdown()))
    table.add_row("Calmar", fmt_ratio(perf.calmar()))
    return table
