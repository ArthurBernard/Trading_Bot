"""Tests for ``trading-bot run <config>`` — the multi-strategy entrypoint path.

These exercise the CLI's declared-system path through Typer's
:class:`~typer.testing.CliRunner`, fully **offline**: a real example-style YAML
config declaring two strategies, with the dccd feed replaced by a fake (via
monkeypatching :func:`trading_bot.application.run_app.feed_for` to an
:class:`~trading_bot.application.data_feed.InMemoryFeed` over canned bars), run
against the engine's :class:`~trading_bot.brokers.paper.PaperBroker`.

What is verified
----------------
* ``run -c <config>`` over a 2-strategy paper config exits 0 and prints a
  **multi-strategy** summary (a per-strategy line for each declared strategy);
* backward compatibility: ``run`` with **no** config still runs the synthetic
  single-strategy demo (exit 0);
* the ``--live`` guard still refuses (non-zero exit, no order placed) for a live
  config without acknowledgement/credentials.
"""

from __future__ import annotations

import pathlib

import polars as pl
import pytest
from typer.testing import CliRunner

from trading_bot.application.config import StrategyConfig
from trading_bot.application.data_feed import BARS_SCHEMA, InMemoryFeed
from trading_bot.interfaces.cli.main import app

runner = CliRunner()

_TWO_STRATEGY_CONFIG = """\
mode: paper
strategies:
  - name: btc-ma
    symbol: BTC/USD
    data:
      exchange: kraken
      span: 60
    signal:
      ref: ma_crossover
      params:
        fast: 3
        slow: 6
    reference_qty: "2"
    lookback: 6
  - name: eth-ma
    symbol: ETH/USD
    data:
      exchange: kraken
      span: 60
    signal:
      ref: ma_crossover
      params:
        fast: 4
        slow: 8
    reference_qty: "3"
    lookback: 8
"""


def _trend(base: float) -> pl.DataFrame:
    """A trend-up-then-down OHLC bars-schema frame (crosses an MA both ways)."""
    up = [base + i for i in range(20)]
    top = base + 19
    down = [top - i for i in range(1, 21)]
    closes = up + down
    n = len(closes)
    return pl.DataFrame(
        {
            "time": [60 * i for i in range(n)],
            "o": closes,
            "h": [c + 0.5 for c in closes],
            "l": [c - 0.5 for c in closes],
            "c": closes,
            "v": [1.0] * n,
        }
    )


def _fake_feed_for_factory() -> object:
    """Build a ``feed_for`` replacement returning a per-symbol InMemoryFeed.

    Keyed by the strategy's ``symbol`` so each declared strategy reads its own
    canned series — the offline stand-in for the dccd read path.
    """
    frames = {
        "BTC/USD": _trend(100.0).select(list(BARS_SCHEMA)),
        "ETH/USD": _trend(50.0).select(list(BARS_SCHEMA)),
    }

    def _feed_for(
        strategy: StrategyConfig,
        *,
        client: object | None = None,
        backfill: bool = False,
        data_path: str | None = None,
    ) -> InMemoryFeed:
        return InMemoryFeed(frames[strategy.symbol])

    return _feed_for


# --- run <config> — the declared multi-strategy system --------------------- #


def test_run_config_two_strategies_prints_multistrategy_summary(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run -c <config>` runs both declared strategies and summarises each.

    The dccd feed is replaced by a fake (InMemoryFeed over canned bars) so the
    whole command is offline; the paper broker is the engine's real data path.
    """
    # Replace the feed seam the entrypoint uses with the offline fake.
    import importlib

    run_app_module = importlib.import_module("trading_bot.application.run_app")
    monkeypatch.setattr(run_app_module, "feed_for", _fake_feed_for_factory())

    cfg = tmp_path / "system.yaml"
    cfg.write_text(_TWO_STRATEGY_CONFIG)

    result = runner.invoke(app, ["run", "-c", str(cfg)])

    assert result.exit_code == 0, result.output
    assert "run complete" in result.output
    assert "mode=paper" in result.output
    assert "strategies=2" in result.output
    # A per-strategy summary line for each declared strategy.
    assert "btc-ma" in result.output
    assert "eth-ma" in result.output
    assert "BTC/USD" in result.output
    assert "ETH/USD" in result.output
    assert "realised PnL" in result.output


def test_run_config_synthetic_path_still_works() -> None:
    """Backward-compat: `run` with no config still runs the synthetic demo."""
    result = runner.invoke(app, ["run"])

    assert result.exit_code == 0, result.output
    assert "run complete" in result.output
    assert "orders submitted" in result.output


# --- --live guard over a declared-system config ---------------------------- #


def test_run_config_live_without_creds_refuses_no_order(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run -c <live-config> --live --yes-i-understand` without creds refuses.

    A config declaring a strategy + a credential-less Kraken venue, opted into
    live (``live_enabled: true``), run ``--live`` with the acknowledgement: both
    CLI gates pass, but ``build_engine`` (inside ``run_app``) refuses the
    credential-less live venue — a non-zero exit, a clear message, no order.
    """
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    # Even if a feed were reached, keep it offline — but the engine refuses first.
    import importlib

    run_app_module = importlib.import_module("trading_bot.application.run_app")
    monkeypatch.setattr(run_app_module, "feed_for", _fake_feed_for_factory())

    cfg = tmp_path / "live.yaml"
    # Opt in (live_enabled) so the CLI's opt-in gate passes and the refusal comes
    # from the credential check inside build_engine.
    cfg.write_text(
        "mode: live\n"
        "live_enabled: true\n"
        "brokers:\n"
        "  - name: k\n"
        "    exchange: kraken\n"
        "strategies:\n"
        "  - name: btc-ma\n"
        "    symbol: BTC/USD\n"
        "    data:\n"
        "      exchange: kraken\n"
        "      span: 60\n"
        "    signal:\n"
        "      ref: ma_crossover\n"
        "      params:\n"
        "        fast: 3\n"
        "        slow: 6\n"
        "    reference_qty: \"1\"\n"
    )

    result = runner.invoke(
        app, ["run", "-c", str(cfg), "--live", "--yes-i-understand"]
    )

    assert result.exit_code != 0
    assert "refusing to run" in result.output
    assert "credentials" in result.output
    assert "run complete" not in result.output


def test_run_config_live_without_ack_refuses() -> None:
    """`run -c <config> --live` with no ack refuses before building anything."""
    result = runner.invoke(app, ["run", "--live"], input="")

    assert result.exit_code != 0
    assert "run complete" not in result.output
