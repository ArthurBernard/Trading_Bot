"""Tests for the ``trading-bot`` CLI commands — ``run`` / ``status`` / ``kpi``.

These exercise the user-facing commands through Typer's
:class:`~typer.testing.CliRunner`, fully **offline** (an in-memory / fixture bars
file replayed through the engine's :class:`~trading_bot.brokers.paper.PaperBroker`
— the engine's real data path):

* ``run`` over a realistic OHLC fixture (MA-crossover, paper) exits 0, prints a
  summary, and moves the position — and the reported PnL matches an *independent*
  recomputation from the broker's own fills (read back from the persisted store);
* the ``--live`` path refuses (non-zero exit, clear message) and **never places
  an order** when confirmation/credentials are missing;
* ``status`` and ``kpi`` render their tables from a persisted store and surface
  the expected position / PnL values.

The ``_render`` helpers are also tested directly (no CLI) from a known state, so
the table formatting is unit-checked without invoking a command.
"""

from __future__ import annotations

import pathlib

import polars as pl
import pytest
from typer.testing import CliRunner

from trading_bot.application.performance_service import PerformanceService
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.domain.position import Position
from trading_bot.interfaces.cli import _render
from trading_bot.interfaces.cli.main import app
from trading_bot.storage.sqlite_store import SqliteStore

runner = CliRunner()

_BTC_USD = Instrument(Symbol("BTC", "USD"))


def _ohlc_fixture(path: pathlib.Path) -> pl.DataFrame:
    """Write a deterministic OHLC CSV that crosses up then down, return the frame.

    A short, explicit series: it rises from 100 to ~120 (the fast MA pulls above
    the slow MA → a long), then falls back below (→ a short/close). Small windows
    in the tests (fast=2, slow=4) make the crossover happen quickly and
    deterministically. ``o/h/l`` track ``c`` (only ``c`` drives the signal).
    """
    closes = [
        100.0, 101.0, 102.0, 104.0, 107.0, 111.0, 116.0, 120.0,
        119.0, 116.0, 112.0, 107.0, 101.0, 95.0, 90.0, 86.0,
    ]
    times = list(range(len(closes)))
    frame = pl.DataFrame(
        {
            "time": times,
            "o": closes,
            "h": [c + 0.5 for c in closes],
            "l": [c - 0.5 for c in closes],
            "c": closes,
            "v": [1.0] * len(closes),
        }
    )
    frame.write_csv(path)
    return frame


# --- run ------------------------------------------------------------------- #


def test_run_over_fixture_paper_moves_position(tmp_path: pathlib.Path) -> None:
    """`run` over an OHLC fixture (paper) exits 0, summarises, and trades.

    Verification on real data: after the run, the PnL the engine reported is
    recomputed *independently* from the broker's fills (read back from the
    persisted store) and must agree exactly — fills are the source of truth.
    """
    pytest.importorskip("fynance")  # the ma_crossover run evaluates fynance.sma
    bars = tmp_path / "bars.csv"
    _ohlc_fixture(bars)
    db = tmp_path / "run.db"

    result = runner.invoke(
        app,
        [
            "run",
            "--bars", str(bars),
            "--db", str(db),
            "--fast", "2",
            "--slow", "4",
            "--qty", "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "run complete" in result.output
    assert "mode=paper" in result.output
    assert "orders submitted" in result.output

    # Independent recomputation from the broker's persisted fills.
    store = SqliteStore(db)
    fills = store.fills()
    assert fills, "the run should have produced at least one fill"

    by_instrument: dict[Instrument, list[Fill]] = {}
    for fill in fills:
        by_instrument.setdefault(fill.instrument, []).append(fill)
    expected_pnl = sum(
        (Position.from_fills(fs).realised_pnl for fs in by_instrument.values()),
        money("0"),
    )

    # The realised PnL printed in the summary must equal the fills-based figure.
    assert _render.fmt_money(expected_pnl) in result.output


def test_run_synthetic_feed_default(tmp_path: pathlib.Path) -> None:
    """`run` with no --bars uses the built-in synthetic feed and exits 0."""
    pytest.importorskip("fynance")  # the synthetic demo uses the ma_crossover signal
    result = runner.invoke(app, ["run"])

    assert result.exit_code == 0, result.output
    assert "run complete" in result.output
    # The default synthetic feed trends enough to submit at least one order.
    assert "orders submitted" in result.output


# --- --live guard ---------------------------------------------------------- #


def test_run_live_without_confirmation_refuses_no_order() -> None:
    """`run --live` with no confirmation refuses (non-zero) and places no order.

    The interactive confirm gets EOF (no tty), so it aborts: a non-zero exit, a
    clear refusal, and — crucially — the live broker is never even built, so no
    order can have been placed.
    """
    result = runner.invoke(app, ["run", "--live"], input="")

    assert result.exit_code != 0
    # No "run complete" line — the run never proceeded to the engine.
    assert "run complete" not in result.output


def test_run_live_without_opt_in_refuses_no_order(
    tmp_path: pathlib.Path,
) -> None:
    """`run --live --yes-i-understand` without ``live_enabled`` refuses, no order.

    The acknowledgement passes the first CLI gate, but the config's off-by-default
    ``live_enabled`` is unset, so the CLI refuses *before building anything*: a
    non-zero exit, a message pointing at the runbook, and no order placed.
    """
    cfg = tmp_path / "live.yaml"
    # mode flipped to live by --live, but live_enabled omitted (defaults False).
    cfg.write_text(
        "mode: paper\nbrokers:\n  - name: k\n    exchange: kraken\n"
    )

    result = runner.invoke(
        app,
        ["run", "--live", "--yes-i-understand", "-c", str(cfg)],
    )

    assert result.exit_code != 0
    assert "refusing to trade live" in result.output
    assert "09-go-live.md" in result.output
    assert "run complete" not in result.output


def test_run_live_acknowledged_opted_in_without_credentials_refuses(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run --live` opted-in but without creds refuses, places no order.

    The acknowledgement *and* ``live_enabled: true`` pass the CLI gates, but
    ``build_engine`` refuses a credential-less live Kraken venue
    (paper-by-default invariant): a non-zero exit, a clear message, no order.
    """
    # Ensure no Kraken credentials are visible.
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)

    cfg = tmp_path / "live.yaml"
    cfg.write_text(
        "mode: live\nlive_enabled: true\n"
        "brokers:\n  - name: k\n    exchange: kraken\n"
    )

    result = runner.invoke(
        app,
        ["run", "--live", "--yes-i-understand", "-c", str(cfg)],
    )

    assert result.exit_code != 0
    assert "refusing to run" in result.output
    assert "credentials" in result.output
    assert "run complete" not in result.output


def test_run_live_declined_confirmation_refuses() -> None:
    """`run --live` answering 'n' to the confirmation refuses with non-zero exit."""
    result = runner.invoke(app, ["run", "--live"], input="n\n")

    assert result.exit_code != 0
    assert "run complete" not in result.output


# --- status ---------------------------------------------------------------- #


def _seed_store(path: pathlib.Path) -> None:
    """Persist a known order + a couple of fills into a store at ``path``."""
    store = SqliteStore(path)

    # A still-open order (status OPEN) so status lists it under "Open orders".
    order = Order(
        client_order_id="cid-1",
        instrument=_BTC_USD,
        side=OrderSide.BUY,
        qty=money("2"),
        type=OrderType.LIMIT,
        limit_price=money("30000"),
    )
    order.submit()
    order.open("VID-1")
    store.upsert_order(order)

    # Fills that leave a net long position of 1 BTC with some realised PnL.
    store.record_fill(
        Fill("F1", "cid-0", _BTC_USD, OrderSide.BUY, money("2"),
             money("30000"), money("0"), 1)
    )
    store.record_fill(
        Fill("F2", "cid-1", _BTC_USD, OrderSide.SELL, money("1"),
             money("31000"), money("0"), 2)
    )


def test_status_renders_positions_and_open_orders(
    tmp_path: pathlib.Path,
) -> None:
    """`status --db` prints a positions table + an open-orders table."""
    db = tmp_path / "state.db"
    _seed_store(db)

    result = runner.invoke(app, ["status", "--db", str(db)])

    assert result.exit_code == 0, result.output
    # Net position 1 BTC, the instrument, and the open order's id all present.
    assert "BTC/USD" in result.output
    assert "Positions" in result.output
    assert "Open orders" in result.output
    assert "cid-1" in result.output


def test_status_missing_db_errors() -> None:
    """`status --db <missing>` fails cleanly rather than crashing."""
    result = runner.invoke(app, ["status", "--db", "/nonexistent/x.db"])

    assert result.exit_code != 0


# --- kpi ------------------------------------------------------------------- #


def test_kpi_renders_realised_pnl(tmp_path: pathlib.Path) -> None:
    """`kpi --db` renders the KPI table with the expected realised PnL.

    A known two-fill sequence (buy 2 @ 30000, sell 1 @ 31000, no fees) realises
    exactly 1000 on the closed unit; the table must show that figure.
    """
    pytest.importorskip("fynance")  # the KPI table computes fynance ratios over the curve
    db = tmp_path / "kpi.db"
    _seed_store(db)

    result = runner.invoke(app, ["kpi", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "Realised PnL" in result.output
    assert "1000" in result.output  # (31000 - 30000) * 1, no fees
    assert "Sharpe" in result.output


def test_kpi_default_capital_anchors_equity_endpoint(
    tmp_path: pathlib.Path,
) -> None:
    """With no ``--capital`` / ``--config`` the built-in 100000 default anchors.

    The realised PnL is 1000 (buy 2 @ 30000, sell 1 @ 31000), so the equity
    endpoint is ``100000 + 1000 = 101000``.
    """
    pytest.importorskip("fynance")  # the KPI table computes fynance ratios over the curve
    db = tmp_path / "kpi.db"
    _seed_store(db)

    result = runner.invoke(app, ["kpi", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "101000" in result.output  # 100000 default + 1000 realised


def test_kpi_explicit_capital_overrides_default(tmp_path: pathlib.Path) -> None:
    """`--capital` wins: equity endpoint anchors to the flag, not the default."""
    pytest.importorskip("fynance")  # the KPI table computes fynance ratios over the curve
    db = tmp_path / "kpi.db"
    _seed_store(db)

    result = runner.invoke(app, ["kpi", "--db", str(db), "--capital", "5000"])

    assert result.exit_code == 0, result.output
    assert "6000" in result.output  # 5000 capital + 1000 realised
    assert "101000" not in result.output  # not the default anchor


def test_kpi_config_starting_capital_used_when_no_capital_flag(
    tmp_path: pathlib.Path,
) -> None:
    """A ``--config`` ``starting_capital`` anchors the curve absent ``--capital``."""
    pytest.importorskip("fynance")  # the KPI table computes fynance ratios over the curve
    db = tmp_path / "kpi.db"
    _seed_store(db)
    cfg = tmp_path / "cfg.yml"
    cfg.write_text("starting_capital: \"200000\"\n")

    result = runner.invoke(app, ["kpi", "--db", str(db), "--config", str(cfg)])

    assert result.exit_code == 0, result.output
    assert "201000" in result.output  # 200000 config + 1000 realised


def test_kpi_capital_flag_beats_config_starting_capital(
    tmp_path: pathlib.Path,
) -> None:
    """Precedence: explicit ``--capital`` > config ``starting_capital``."""
    pytest.importorskip("fynance")  # the KPI table computes fynance ratios over the curve
    db = tmp_path / "kpi.db"
    _seed_store(db)
    cfg = tmp_path / "cfg.yml"
    cfg.write_text("starting_capital: \"200000\"\n")

    result = runner.invoke(
        app, ["kpi", "--db", str(db), "--config", str(cfg), "--capital", "5000"]
    )

    assert result.exit_code == 0, result.output
    assert "6000" in result.output  # 5000 flag wins
    assert "201000" not in result.output  # not the config anchor


# --- _render helpers (no CLI) ---------------------------------------------- #


def test_positions_table_contains_formatted_values() -> None:
    """`positions_table` renders net qty / avg entry / realised PnL exactly."""
    fills = [
        Fill("F1", "c-0", _BTC_USD, OrderSide.BUY, money("2"),
             money("30000"), money("0"), 1),
        Fill("F2", "c-1", _BTC_USD, OrderSide.SELL, money("1"),
             money("31000"), money("0"), 2),
    ]
    pos = Position.from_fills(fills)
    table = _render.positions_table({_BTC_USD: pos})

    rendered = _render_to_text(table)
    assert "BTC/USD" in rendered
    assert "1000" in rendered  # realised PnL
    assert "30000" in rendered  # avg entry of the remaining 1 BTC


def test_kpi_table_contains_realised_pnl_and_ratios() -> None:
    """`kpi_table` shows realised PnL + the named ratios for a known fill set."""
    pytest.importorskip("fynance")  # kpi_table computes fynance ratios over the curve
    perf = PerformanceService(v0=money("100000"))
    perf.apply(
        Fill("F1", "c-0", _BTC_USD, OrderSide.BUY, money("2"),
             money("30000"), money("0"), 1)
    )
    perf.apply(
        Fill("F2", "c-1", _BTC_USD, OrderSide.SELL, money("1"),
             money("31000"), money("0"), 2)
    )
    table = _render.kpi_table(perf)

    rendered = _render_to_text(table)
    assert "Realised PnL" in rendered
    assert "1000" in rendered
    for metric in ("Sharpe", "Sortino", "Max drawdown", "Calmar"):
        assert metric in rendered


def test_fmt_money_never_uses_float() -> None:
    """`fmt_money` renders exact decimals (no float) and ``None`` as a dash."""
    assert _render.fmt_money(money("0.1")) == "0.1"
    assert _render.fmt_money(money("1000")) == "1000"
    assert _render.fmt_money(None) == "-"
    # Fixed-places display quantises exactly, no binary error.
    assert _render.fmt_money(money("1000.5"), places=2) == "1000.50"


def _render_to_text(renderable: object) -> str:
    """Render a rich renderable to plain text for substring assertions."""
    from rich.console import Console

    console = Console(width=200, record=True)
    console.print(renderable)
    return console.export_text()
