"""Tests for the :class:`StrategySupervisor` — per-strategy lifecycle + modes.

Offline: a paper config + a fake dccd client (canned bars). Proves the supervisor
splits a config into independently-managed units, starts/steps/stops them in their
own engine, switches modes (paper ↔ testnet), and **gates real money** (``live``
needs an explicit confirmation). Async tests run un-decorated (``asyncio_mode =
"auto"``).
"""

from __future__ import annotations

import polars as pl
import pytest

from trading_bot.application.config import AppConfig
from trading_bot.application.supervisor import StrategySupervisor
from trading_bot.domain.errors import ConfigError, LiveTradingNotEnabled


def _dccd_ohlc(closes: list[float], *, span_s: int = 60) -> pl.DataFrame:
    span_ns = span_s * 1_000_000_000
    ts = [i * span_ns for i in range(len(closes))]
    return pl.DataFrame(
        {
            "TS": ts,
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1.0] * len(closes),
            "quote_volume": list(closes),
            "trades": [1] * len(closes),
        }
    )


class _FakeDccdClient:
    """A canned offline dccd client keyed by symbol (no network)."""

    def __init__(self, frames: dict[str, pl.DataFrame]) -> None:
        self._frames = frames

    def read(self, exchange, symbol, data_type="ohlc", span=None, start_ns=None, end_ns=None):  # noqa: ANN001, ANN201
        return self._frames[symbol]

    def backfill(self, *a, **k):  # noqa: ANN002, ANN003, ANN201  # pragma: no cover
        return None


def _trend() -> list[float]:
    """A close series that trends up then down (the MA crosses both ways)."""
    return [100.0 + i for i in range(20)] + [119.0 - i for i in range(1, 21)]


def _config(*, with_broker: bool = True) -> AppConfig:
    """A paper config: one BTC/USD MA-crossover strategy (+ an optional broker)."""
    raw: dict = {
        "mode": "paper",
        "strategies": [
            {
                "name": "btc-ma",
                "symbol": "BTC/USD",
                "data": {"exchange": "kraken", "span": 60},
                "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                "reference_qty": "2",
                "lookback": 6,
            }
        ],
    }
    if with_broker:
        raw["brokers"] = [{"name": "kraken", "exchange": "kraken"}]
    return AppConfig.model_validate(raw)


def _supervisor() -> StrategySupervisor:
    client = _FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())})
    return StrategySupervisor(_config(), dccd_client=client)


def test_splits_config_into_units() -> None:
    """The supervisor splits the config into one (stopped, paper) unit per strategy."""
    sup = _supervisor()
    assert sup.names() == ["btc-ma"]
    [status] = sup.status()
    assert status.name == "btc-ma"
    assert status.kind == "strategy"
    assert status.mode == "paper"
    assert status.running is False
    assert status.realised_pnl is None


async def test_start_step_stop_lifecycle() -> None:
    """start builds a per-unit engine; step re-evaluates over latest data; stop tears down."""
    pytest.importorskip("fynance")  # ma_crossover evaluates fynance.sma
    sup = _supervisor()

    await sup.start("btc-ma")
    assert sup.status("btc-ma")[0].running is True

    # One re-evaluation over the latest (trend-down tail → short) data trades.
    order = await sup.step("btc-ma")
    assert order is not None  # delta != 0 from flat → an order was routed

    await sup.stop("btc-ma")
    status = sup.status("btc-ma")[0]
    assert status.running is False
    # Stopped → nothing to step.
    assert await sup.step("btc-ma") is None


async def test_set_mode_paper_testnet_roundtrip() -> None:
    """paper ↔ testnet switch needs no confirmation and updates the unit's mode."""
    sup = _supervisor()
    await sup.set_mode("btc-ma", "testnet")
    assert sup.status("btc-ma")[0].mode == "testnet"
    await sup.set_mode("btc-ma", "paper")
    assert sup.status("btc-ma")[0].mode == "paper"


async def test_set_mode_live_requires_explicit_confirmation() -> None:
    """Switching to live (real money) without confirmation is refused."""
    sup = _supervisor()
    with pytest.raises(LiveTradingNotEnabled):
        await sup.set_mode("btc-ma", "live")  # no confirm → refused
    assert sup.status("btc-ma")[0].mode == "paper"  # unchanged

    # With the deliberate acknowledgement the mode flips (the engine is only built
    # on start, which still enforces credentials + risk limits).
    await sup.set_mode("btc-ma", "live", confirm_live=True)
    assert sup.status("btc-ma")[0].mode == "live"


async def test_testnet_without_a_broker_is_refused() -> None:
    """A paper-only unit with no configured broker cannot go testnet/live."""
    sup = StrategySupervisor(
        _config(with_broker=False),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    with pytest.raises(ConfigError, match="without a configured broker"):
        await sup.set_mode("btc-ma", "testnet")


async def test_unknown_strategy_is_a_config_error() -> None:
    """Operating on an unknown name raises a clear error."""
    sup = _supervisor()
    with pytest.raises(ConfigError, match="unknown strategy"):
        await sup.start("nope")


async def test_start_all_step_all_shutdown() -> None:
    """The daemon's boot/tick/teardown: start every unit, step the running ones, stop."""
    pytest.importorskip("fynance")
    sup = _supervisor()

    await sup.start_all()
    assert all(s.running for s in sup.status())

    stepped = await sup.step_all()
    assert stepped == 1  # the one running unit stepped once

    await sup.shutdown()
    assert not any(s.running for s in sup.status())
    assert await sup.step_all() == 0  # nothing running → nothing stepped
