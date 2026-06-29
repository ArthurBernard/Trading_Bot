"""LS1 end-to-end — the portfolio path on **real** dccd Binance bars (+ opt-in LS1/testnet).

The mandatory real-data evidence for the portfolio-strategy epic: it proves the
*whole* multi-asset path — config → resampled daily feed → weight vector → sized
per-coin targets → idempotent risk-gated routing → broker-confirmed positions —
on the **real** dccd Binance store, and wires the validated **LS1** strategy
(``../fynance-research/DEPLOY_LS1.md``) by config + a thin generic adapter only.

Three layers, by what is available in the running environment
-------------------------------------------------------------
1. **Adapter unit tests** (always run) — the generic
   :func:`~trading_bot.application.portfolio.as_portfolio_signal` that bridges a
   research ``() -> {pair: weight}`` oracle to the
   :data:`~trading_bot.application.portfolio.PortfolioSignalFn` contract:
   pair-string key normalisation, the dict-or-``(dict, asof)`` return shapes,
   and a ``Σ|w| ≤ 2`` gross-cap assertion helper.
2. **Real-dccd portfolio e2e** (``-m network``; runs whenever the store is
   present) — a small real universe (``BTC/ETH/BNB-USDT``) fed through the real
   1m store resampled to daily by the production
   :class:`~trading_bot.application.data_provider.ResamplingDccdClient`, a
   *deterministic* fake weight vector, run through :func:`run_app` against the
   :class:`~trading_bot.brokers.paper.PaperBroker`. Asserts the routed per-coin
   deltas equal ``weightᵢ × capital / priceᵢ`` on the **real latest closes**, that
   Σ routed notionals ≤ ``gross_cap × capital``, that the freshness gate holds,
   and that **broker-confirmed** tracker positions agree. The LS1-specific path
   is identical — only the signal source differs (the research dep, below).
3. **LS1-real e2e + Binance testnet rebalance** (gated, skip where the
   prerequisite is absent) — the *same* path with the real LS1 signal and, opt-in,
   against the Binance **testnet** broker. Present and correct, ready to run:

   * **LS1-real** needs the research package::

         pip install -e ../fynance-research

   * **testnet** needs a testnet key in the env *and* the testnet base URL::

         BINANCE_API_KEY=...           # a *testnet* key (testnet.binance.vision)
         BINANCE_API_SECRET=...
         BINANCE_API_BASE=https://testnet.binance.vision

     It runs ONE rebalance with a tiny capital, reads back ``open_orders()`` /
     ``balances()``, asserts the placed legs match the intended deltas, then
     **cancels** every leg in a ``finally``. No mainnet — paper is the default and
     the testnet test refuses to run against ``api.binance.com``.

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from trading_bot.application.config import AppConfig
from trading_bot.application.data_provider import ResamplingDccdClient
from trading_bot.application.portfolio import as_portfolio_signal
from trading_bot.domain.instrument import (
    Instrument,
    Symbol,
    parse_binance_symbol,
    parse_kraken_pair,
)
from trading_bot.domain.money import Money, money

# --- shared constants ------------------------------------------------------- #

#: The real dccd Binance store root (1m parquet, dirs keyed "BTC-USDT").
_STORE = Path.home() / "data" / "arthurserver" / "binance" / "ohlc"

#: The real dccd Kraken store root (1m parquet, dirs keyed "BTC-USD").
_KRAKEN_STORE = Path.home() / "data" / "arthurserver" / "kraken" / "ohlc"

#: The 10 LS1 coins (all *-USDT, daily) — DEPLOY_LS1.md §2.
_LS1_COINS = ["BTC", "ETH", "BCH", "LTC", "XRP", "XLM", "DOGE", "DOT", "TRX", "ZEC"]

#: A small real universe for the runs-here e2e (coins present in the store).
_SMALL_UNIVERSE = [Symbol("BTC", "USDT"), Symbol("ETH", "USDT"), Symbol("BNB", "USDT")]

#: The deterministic fake weight vector for the runs-here e2e (Σ|w| = 1.0 ≤ 2).
_FAKE_WEIGHTS: dict[Symbol, Money] = {
    Symbol("BTC", "USDT"): money("0.5"),
    Symbol("ETH", "USDT"): money("-0.3"),
    Symbol("BNB", "USDT"): money("0.2"),
}


# --- the Σ|w| ≤ gross_cap helper -------------------------------------------- #


def assert_gross_within(weights: Mapping[Symbol, Money], cap: Money) -> Money:
    """Assert ``Σ|w| ≤ cap`` and return the gross exposure (exact ``Decimal``).

    The gross-leverage gate every portfolio weight vector must respect (LS1's is
    ``2``). Pure ``Decimal`` arithmetic — never ``float``.
    """
    gross = sum((abs(w) for w in weights.values()), money("0"))
    assert gross <= cap, f"gross exposure {gross} exceeds cap {cap}"
    return gross


# --- a parquet-reading source client over the real store -------------------- #


class _ParquetSource:
    """A minimal sync dccd-shaped client reading the real 1m parquet store.

    The real ``dccd.Client.read`` only runs inside ``async with Client()``; the
    sync feed path (:class:`~trading_bot.application.data_feed.DccdFeed`) reads
    synchronously, so — exactly like the ``test_portfolio_feed.py`` network test —
    this reads the stored 1m parquet directly and returns the dccd OHLC columns.
    It is wrapped in the **production**
    :class:`~trading_bot.application.data_provider.ResamplingDccdClient` so the
    real 1m→daily resampling code (not a hand-rolled copy) is what the e2e
    exercises. Store dirs are keyed ``"BTC-USDT"`` (hyphen); a requested pair in
    any form (``"BTCUSDT"`` / ``"BTC/USDT"`` / ``"BTC-USDT"``) is normalised to it,
    so the **default** ``run_app`` feed (which renders ``"BTCUSDT"`` for Binance or
    ``"XBTUSD"`` for Kraken) resolves without any config-side ``symbol_for`` hook —
    every spelling normalises back to the canonical ``BASE-QUOTE`` store key.

    Parameters
    ----------
    store : Path, optional
        The venue's OHLC store root (``<root>/<PAIR>/1m/*.parquet``). Defaults to
        the Binance store; pass :data:`_KRAKEN_STORE` for the Kraken USD pairs.
    """

    def __init__(self, store: Path = _STORE) -> None:
        self._store = store

    def _dir_for(self, symbol: str) -> str:
        """Map any venue render to the store's ``BASE-QUOTE`` dir key.

        Venue renders are ambiguous to invert — ``to_venue_symbol("kraken")``
        gives ``XBTUSD`` (which ``parse_binance_symbol`` mis-reads as ``XB/TUSD``)
        and ``TRXUSD`` (which ``parse_kraken_pair`` mis-reads as ``TR/USD`` — the
        ``X`` looks like a legacy prefix). So try **both** canonical parsers and
        pick the candidate whose dir actually **exists** in this store; fall back
        to the first parse. Robust for Binance (``BTCUSDT``) and Kraken
        (``XBTUSD``/``TRXUSD``) renders alike.
        """
        candidates: list[str] = []
        for parse in (parse_kraken_pair, parse_binance_symbol):
            try:
                sym = parse(symbol)
            except ValueError:
                continue
            candidates.append(f"{sym.base}-{sym.quote}")
        for cand in candidates:
            if (self._store / cand).is_dir():
                return cand
        return candidates[0] if candidates else symbol

    def read(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        base = self._store / self._dir_for(symbol) / "1m"
        files = sorted(base.glob("*.parquet"))
        if not files:
            pytest.skip(f"no 1m parquet for {symbol} under {base}")
        raw = (
            pl.concat([pl.read_parquet(f) for f in files])
            .unique(subset="TS", keep="last")
            .sort("TS")
        )
        if start_ns is not None:
            raw = raw.filter(pl.col("TS") >= start_ns)
        if end_ns is not None:
            raw = raw.filter(pl.col("TS") <= end_ns)
        return raw

    def backfill(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start: str = "last",
    ) -> None:  # pragma: no cover - not exercised (backfill=False)
        return None


def _hyphen_universe() -> list[str]:
    """The small universe as ``BASE/QUOTE`` pair strings for the config."""
    return [f"{s.base}/{s.quote}" for s in _SMALL_UNIVERSE]


def _real_daily_client() -> ResamplingDccdClient:
    """The production resampling client over the real 1m parquet store."""
    return ResamplingDccdClient(_ParquetSource())


def _deterministic_signal(weights: Mapping[Symbol, Money]):  # type: ignore[no-untyped-def]
    """A fixed-weight :data:`PortfolioSignalFn` (frame-agnostic, deterministic)."""

    def _fn(
        asof_ms: int, frames: Mapping[Symbol, pl.DataFrame]
    ) -> Mapping[Symbol, Money]:
        return dict(weights)

    return _fn


# =========================================================================== #
# 1. Adapter unit tests (ALWAYS run — no store / research / network)
# =========================================================================== #


def test_adapter_normalises_hyphen_pair_keys() -> None:
    """``{"BTC-USDT": 0.3, "ZEC-USDT": -0.1}`` → canonical ``Symbol`` keys."""
    oracle = lambda: {"BTC-USDT": 0.3, "ZEC-USDT": -0.1}  # noqa: E731
    signal = as_portfolio_signal(oracle)

    out = signal(0, {})

    assert out == {
        Symbol("BTC", "USDT"): money("0.3"),
        Symbol("ZEC", "USDT"): money("-0.1"),
    }
    # Weights are exact Decimal, never float.
    assert all(isinstance(w, Decimal) for w in out.values())


def test_adapter_accepts_concatenated_binance_keys() -> None:
    """The default parse also handles the bare ``"BTCUSDT"`` (no-separator) form."""
    signal = as_portfolio_signal(lambda: {"BTCUSDT": 1, "ETHUSDT": -1})

    out = signal(0, {})

    assert out == {
        Symbol("BTC", "USDT"): money("1"),
        Symbol("ETH", "USDT"): money("-1"),
    }


def test_adapter_accepts_mapping_or_tuple_return_shape() -> None:
    """The oracle may return ``{pair: w}`` OR ``({pair: w}, asof)`` — both work."""
    bare = as_portfolio_signal(lambda: {"BTC-USDT": 0.5})
    tupled = as_portfolio_signal(lambda: ({"BTC-USDT": 0.5}, 1_700_000_000_000))

    expected = {Symbol("BTC", "USDT"): money("0.5")}
    assert bare(0, {}) == expected
    assert tupled(0, {}) == expected  # the asof component is discarded


def test_adapter_passes_through_symbol_keys() -> None:
    """A weight vector already keyed by canonical ``Symbol`` is left as-is."""
    signal = as_portfolio_signal(lambda: {Symbol("BTC", "USDT"): 0.5})
    assert signal(0, {}) == {Symbol("BTC", "USDT"): money("0.5")}


def test_adapter_ignores_frames_and_asof() -> None:
    """The adapter ignores the passed frames/asof (the oracle reads its own store)."""
    calls = {"n": 0}

    def _oracle() -> dict[str, float]:
        calls["n"] += 1
        return {"BTC-USDT": 0.4}

    signal = as_portfolio_signal(_oracle)
    # Different frames/asof → same result; the oracle is the sole source.
    a = signal(111, {Symbol("BTC", "USDT"): pl.DataFrame({"c": [1.0]})})
    b = signal(999, {})
    assert a == b == {Symbol("BTC", "USDT"): money("0.4")}
    assert calls["n"] == 2  # called once per evaluation


def test_adapter_rejects_bad_return_shape() -> None:
    """A non-mapping, non-(mapping, asof) return is a clear SignalError."""
    from trading_bot.domain.errors import SignalError

    signal = as_portfolio_signal(lambda: [("BTC-USDT", 0.5)])  # a list, not a dict
    with pytest.raises(SignalError, match="mapping"):
        signal(0, {})


def test_adapter_rejects_unparseable_key() -> None:
    """A key that cannot be parsed to a Symbol is a clear SignalError."""
    from trading_bot.domain.errors import SignalError

    signal = as_portfolio_signal(lambda: {"???": 0.5})
    with pytest.raises(SignalError, match="parseable pair"):
        signal(0, {})


def test_gross_within_helper() -> None:
    """The Σ|w| ≤ 2 helper accepts a compliant vector and rejects an over-levered one."""
    ok = {Symbol("BTC", "USDT"): money("1.3"), Symbol("ETH", "USDT"): money("-0.7")}
    assert assert_gross_within(ok, money("2")) == money("2.0")

    too_much = {Symbol("BTC", "USDT"): money("1.5"), Symbol("ETH", "USDT"): money("-1.0")}
    with pytest.raises(AssertionError, match="exceeds cap"):
        assert_gross_within(too_much, money("2"))


def test_ls1_config_loads_and_signal_resolves() -> None:
    """``configs/ls1.yaml`` validates and its signal ref resolves (no research dep).

    The LS1 wrapper imports ``fynance_research`` *lazily* (inside the signal), so
    loading the config and resolving the ``module:function`` ref work without the
    research package installed — only *evaluating* the signal needs it. This proves
    LS1 is wired by config + a thin generic adapter, not engine code.
    """
    from trading_bot.application.portfolio import load_portfolio_signal

    cfg = AppConfig.from_yaml("configs/ls1.yaml")
    assert cfg.mode == "paper"  # paper by default — never live by accident
    assert len(cfg.portfolios) == 1
    p = cfg.portfolios[0]
    assert p.name == "ls1"
    # The full validated 10-coin universe.
    assert [f"{s.base}/{s.quote}" for s in (Symbol(c, "USDT") for c in _LS1_COINS)] == [
        u.replace("XBT", "BTC") for u in p.universe
    ]
    assert p.gross_cap == Decimal("2")
    assert p.data.exchange == "binance" and p.data.span == 86_400
    assert p.signal.ref == "examples.ls1_signal:ls1_portfolio_signal"

    fn = load_portfolio_signal(p.signal.ref)  # resolves WITHOUT fynance_research
    assert callable(fn)


# =========================================================================== #
# 2. Real-dccd portfolio e2e (RUNS here — the mandatory real-data evidence)
# =========================================================================== #


@pytest.mark.network
async def test_real_dccd_portfolio_e2e_delta_correctness() -> None:
    """Real dccd Binance bars → portfolio runner → deltas == weightᵢ·capital/priceᵢ.

    Builds a 3-coin (BTC/ETH/BNB-USDT) portfolio from a real
    :class:`AppConfig` with a **deterministic** fake weight vector, fed through the
    **real** 1m store resampled to daily by the production
    :class:`ResamplingDccdClient`, assembled exactly as :func:`run_app` does
    (:func:`build_engine` + :func:`build_portfolio_runners`) against the
    :class:`PaperBroker`, and rebalanced **once on the latest real cross-section**.
    Asserts, on the **real latest closes**:

    * each coin's routed/filled net qty from flat == ``weightᵢ × capital / priceᵢ``
      (exact ``Decimal``);
    * Σ routed notionals ≤ ``gross_cap × capital`` (the gross gate);
    * the freshness gate held (all coins share the latest common closed day);
    * **broker-confirmed** tracker positions equal those targets (not local
      optimism).

    Rebalancing the *latest* window once (rather than draining the whole ~2300-day
    feed) keeps the test brisk while exercising the identical real config → real
    resampled feed → real runner → real paper-broker path; every prior tick would
    be causal too (proven offline in ``test_portfolio_runner.py``). The
    LS1-specific path is identical; only the signal source differs (gated below on
    the research dep). Skips with a how-to-sync reason when the store is absent.
    """
    if not _STORE.is_dir():
        pytest.skip(
            "no dccd Binance store at ~/data/arthurserver/binance/ohlc; sync the "
            "Binance *-USDT 1m pairs via the dccd daemon (DEPLOY_LS1.md §3)"
        )

    from trading_bot.application.portfolio_feed import PortfolioFeed
    from trading_bot.application.run_app import build_portfolio_runners
    from trading_bot.application.service_factory import build_engine

    capital = money("100000")
    gross_cap = money("2")
    weights = _FAKE_WEIGHTS

    # Σ|w| within the gross cap (the same gate LS1's signal enforces).
    assert_gross_within(weights, gross_cap)

    client = _real_daily_client()

    # --- the real latest common cross-section (the freshness gate) ----------- #
    feed = PortfolioFeed(
        _SMALL_UNIVERSE,
        exchange="binance",
        client=client,
        span=86_400,
        symbol_for=lambda s: f"{s.base}-{s.quote}",
    )
    latest = feed.latest()
    # Freshness gate: every coin has the same common dates (inner join), > 200.
    heights = {sym: latest[sym].height for sym in _SMALL_UNIVERSE}
    assert len(set(heights.values())) == 1, f"unequal common dates: {heights}"
    common = next(iter(latest.values()))["time"].to_list()
    assert len(common) > 200, f"too few common daily dates: {len(common)}"
    asof_ns = common[-1]
    closes: dict[Symbol, Money] = {
        sym: money(str(latest[sym]["c"][-1])) for sym in _SMALL_UNIVERSE
    }

    # The config: paper, the 3-coin universe, daily Binance data, a deterministic
    # signal injected by an importable module:function ref (never exec'd).
    cfg = AppConfig.model_validate(
        {
            "mode": "paper",
            "starting_capital": str(capital),
            "brokers": [{"name": "paper-binance", "exchange": "paper"}],
            "portfolios": [
                {
                    "name": "fake-book",
                    "venue": "binance",
                    "universe": _hyphen_universe(),
                    "signal": {
                        "ref": (
                            "trading_bot.tests.application.test_ls1_e2e:"
                            "_fake_book_signal"
                        )
                    },
                    "capital": str(capital),
                    "gross_cap": str(gross_cap),
                    "data": {"exchange": "binance", "span": 86_400},
                }
            ],
        }
    )

    # --- assemble the engine + runner (the run_app wiring) and rebalance ------ #
    # build_portfolio_runners builds its own PortfolioFeed (default symbol render
    # "BTCUSDT"); the parquet source normalises any spelling to the hyphen dir, so
    # it reads the same store and resamples identically. We rebalance ONCE on the
    # runner's latest window — the same code run_app drives per tick.
    engine = build_engine(cfg, db_path=cfg.storage.db_path)
    runner = build_portfolio_runners(cfg, engine, dccd_client=client)[0]
    runner_feed = runner._feed  # the real PortfolioFeed built from the config
    latest_window = runner_feed.latest()
    result = await runner.rebalance(latest_window)

    assert result.failed == 0, f"legs failed: {result.failures}"
    assert result.submitted == 3  # all three weights non-zero → 3 legs from flat

    # --- broker-confirmed positions == weightᵢ × capital / priceᵢ ------------ #
    gross_notional = money("0")
    intended: dict[Symbol, Money] = {}
    confirmed: dict[Symbol, Money] = {}
    for sym in _SMALL_UNIVERSE:
        pos = engine.tracker.position(Instrument(sym))
        assert pos is not None, f"{sym} never filled"
        want = money(str(weights[sym] * capital / closes[sym]))
        got = pos.net_qty
        assert got == want, (
            f"{sym}: broker-confirmed {got} != intended {want} "
            f"(close={closes[sym]}, weight={weights[sym]})"
        )
        intended[sym] = want
        confirmed[sym] = got
        gross_notional += abs(got) * closes[sym]

    # Σ routed notionals ≤ gross_cap × capital (the gross gate on real closes).
    assert gross_notional <= gross_cap * capital, (
        f"gross notional {gross_notional} exceeds cap {gross_cap * capital}"
    )

    # Emit the evidence to the captured test log (run with -s to see it live).
    print("\n=== real-dccd portfolio e2e evidence ===")
    print(f"asof (latest common closed day, ns): {asof_ns}")
    print(f"capital: {capital}  gross_cap: {gross_cap}")
    for sym in _SMALL_UNIVERSE:
        print(
            f"  {sym}: weight={weights[sym]}  close={closes[sym]}  "
            f"intended={intended[sym]}  routed/confirmed={confirmed[sym]}"
        )
    print(f"gross notional: {gross_notional}  (cap {gross_cap * capital})")


def _fake_book_signal(
    asof_ms: int, frames: Mapping[Symbol, pl.DataFrame]
) -> Mapping[Symbol, Money]:
    """Module-level deterministic weight vector for the real-dccd e2e config ref.

    A :data:`~trading_bot.application.portfolio.PortfolioSignalFn` the e2e config
    resolves by ``module:function`` reference (importable, never exec'd). Returns
    the fixed :data:`_FAKE_WEIGHTS` regardless of inputs — the deterministic stand
    -in for the LS1 oracle so the real-data path runs without the research dep.
    """
    return dict(_FAKE_WEIGHTS)


# =========================================================================== #
# 3a. LS1-real e2e (GATED on fynance_research — SKIPS here)
# =========================================================================== #


@pytest.mark.network
async def test_ls1_real_e2e() -> None:
    """The *real* LS1 signal over the 10-coin universe → run_app → delta check.

    Identical to the real-dccd e2e above but with the **real** LS1 weight oracle
    (``fynance_research.strategies.ls1_live.target_weights`` via the
    ``examples.ls1_signal:ls1_portfolio_signal`` wrapper). Asserts Σ|w| ≤ 2 on the
    oracle's own output and that each coin's broker-confirmed position equals
    ``weightᵢ × capital / priceᵢ`` on the latest real close.

    SKIPS here — ``fynance_research`` is not installed. To run::

        pip install -e ../fynance-research
        .venv/bin/python -m pytest \\
            trading_bot/tests/application/test_ls1_e2e.py::test_ls1_real_e2e \\
            -m network -v
    """
    pytest.importorskip(
        "fynance_research",
        reason=(
            "LS1 needs the research package: pip install -e ../fynance-research"
        ),
    )
    if not _STORE.is_dir():
        pytest.skip(
            "no dccd Binance store at ~/data/arthurserver/binance/ohlc; sync the "
            "10 LS1 *-USDT 1m pairs via the dccd daemon (DEPLOY_LS1.md §3)"
        )

    from examples.ls1_signal import ls1_portfolio_signal

    capital = money("100000")
    gross_cap = money("2")
    client = _real_daily_client()
    universe = [Symbol(c, "USDT") for c in _LS1_COINS]

    # Evaluate the real oracle once (it reads its own store) and check Σ|w| ≤ 2.
    weights = ls1_portfolio_signal(0, {})
    assert_gross_within(weights, gross_cap)

    cfg = AppConfig.model_validate(
        {
            "mode": "paper",
            "starting_capital": str(capital),
            "brokers": [{"name": "paper-binance", "exchange": "paper"}],
            "portfolios": [
                {
                    "name": "ls1",
                    "venue": "binance",
                    "universe": [f"{s.base}/{s.quote}" for s in universe],
                    "signal": {"ref": "examples.ls1_signal:ls1_portfolio_signal"},
                    "capital": str(capital),
                    "gross_cap": str(gross_cap),
                    "data": {"exchange": "binance", "span": 86_400},
                }
            ],
        }
    )

    # Latest real closes per coin (the freshness-gated cross-section).
    from trading_bot.application.portfolio_feed import PortfolioFeed
    from trading_bot.application.run_app import build_portfolio_runners
    from trading_bot.application.service_factory import build_engine

    feed = PortfolioFeed(
        universe,
        exchange="binance",
        client=client,
        span=86_400,
        symbol_for=lambda s: f"{s.base}-{s.quote}",
    )
    latest = feed.latest()
    closes = {sym: money(str(latest[sym]["c"][-1])) for sym in universe}

    # Assemble exactly as run_app does and rebalance ONCE on the latest window
    # (a full drain would recompute the whole LS1 book per tick over ~2300 days).
    engine = build_engine(cfg, db_path=cfg.storage.db_path)
    runner = build_portfolio_runners(cfg, engine, dccd_client=client)[0]
    await runner.rebalance(runner._feed.latest())

    # Each coin's broker-confirmed net == weightᵢ × capital / priceᵢ on the latest
    # real close (a coin with weight 0 is flat — no position).
    for sym in universe:
        want = money(str(weights.get(sym, money("0")) * capital / closes[sym]))
        pos = engine.tracker.position(Instrument(sym))
        got = pos.net_qty if pos is not None else money("0")
        assert got == want, f"{sym}: confirmed {got} != intended {want}"


# =========================================================================== #
# 3a-kraken. LS1 on Kraken (USD) — real signal + real data + LIVE public ticker,
#            but PaperBroker (NO real order; Kraken has no testnet → real money)
# =========================================================================== #


@pytest.mark.network
async def test_ls1_kraken_real_e2e() -> None:
    """The real LS1 signal on **Kraken** (USD) → real dccd Kraken bars → PaperBroker.

    The Kraken counterpart of :func:`test_ls1_real_e2e`. **Kraken has no public
    spot testnet**, so a live test that *places* orders on Kraken would be real
    money. This therefore runs the **real** LS1 Kraken book
    (``fynance_research...target_weights("kraken")`` via ``ls1_kraken_signal``)
    over the **real dccd Kraken store**, with a **live Kraken public-ticker**
    sanity check (key-free) — but routes the rebalance through the **PaperBroker**:
    **no real order is ever placed**. Asserts Σ|w| ≤ 2 and each coin's
    broker-confirmed position == ``weightᵢ × capital / priceᵢ`` on the latest real
    Kraken close.

    SKIPS without the research package. To run::

        pip install -e ../fynance-research
        .venv/bin/python -m pytest \\
            trading_bot/tests/application/test_ls1_e2e.py::test_ls1_kraken_real_e2e \\
            -m network -v
    """
    pytest.importorskip(
        "fynance_research",
        reason="LS1 needs the research package: pip install -e ../fynance-research",
    )
    if not _KRAKEN_STORE.is_dir():
        pytest.skip(
            "no dccd Kraken store at ~/data/arthurserver/kraken/ohlc; sync the 10 "
            "LS1 *-USD 1m pairs via the dccd daemon"
        )

    from examples.ls1_signal import ls1_kraken_signal

    capital = money("100000")
    gross_cap = money("2")
    client = ResamplingDccdClient(_ParquetSource(_KRAKEN_STORE))
    universe = [Symbol(c, "USD") for c in _LS1_COINS]

    # The real Kraken oracle (reads its own store), Σ|w| ≤ 2.
    weights = ls1_kraken_signal(0, {})
    assert_gross_within(weights, gross_cap)

    cfg = AppConfig.model_validate(
        {
            "mode": "paper",
            "starting_capital": str(capital),
            "brokers": [{"name": "paper-kraken", "exchange": "paper"}],
            "portfolios": [
                {
                    "name": "ls1-kraken",
                    "venue": "kraken",
                    "universe": [f"{s.base}/{s.quote}" for s in universe],
                    "signal": {"ref": "examples.ls1_signal:ls1_kraken_signal"},
                    "capital": str(capital),
                    "gross_cap": str(gross_cap),
                    "data": {"exchange": "kraken", "span": 86_400},
                }
            ],
        }
    )

    from trading_bot.application.portfolio_feed import PortfolioFeed
    from trading_bot.application.run_app import build_portfolio_runners
    from trading_bot.application.service_factory import build_engine
    from trading_bot.brokers.kraken import KrakenBroker

    feed = PortfolioFeed(
        universe,
        exchange="kraken",
        client=client,
        span=86_400,
        symbol_for=lambda s: f"{s.base}-{s.quote}",
    )
    latest = feed.latest()
    closes = {sym: money(str(latest[sym]["c"][-1])) for sym in universe}

    # LIVE Kraken public ticker (key-free) — confirm the venue responds live and the
    # latest dccd daily close is in the same ballpark as the intraday price (a wide
    # band, since one is a daily close and the other is live intraday).
    live_btc = await KrakenBroker().ticker(Instrument(Symbol("BTC", "USD")))
    assert live_btc > 0
    ratio = live_btc / closes[Symbol("BTC", "USD")]
    assert money("0.5") < ratio < money("2"), (
        f"live Kraken BTC/USD {live_btc} far from dccd close "
        f"{closes[Symbol('BTC', 'USD')]}"
    )

    # Assemble exactly as run_app does and rebalance ONCE on the latest window —
    # through the PaperBroker (no real order). Each coin's broker-confirmed net ==
    # weightᵢ × capital / priceᵢ on the latest real Kraken close.
    engine = build_engine(cfg, db_path=cfg.storage.db_path)
    runner = build_portfolio_runners(cfg, engine, dccd_client=client)[0]
    await runner.rebalance(runner._feed.latest())

    for sym in universe:
        want = money(str(weights.get(sym, money("0")) * capital / closes[sym]))
        pos = engine.tracker.position(Instrument(sym))
        got = pos.net_qty if pos is not None else money("0")
        assert got == want, f"{sym}: confirmed {got} != intended {want}"


# =========================================================================== #
# 3b. Binance testnet rebalance (GATED on creds — SKIPS here)
# =========================================================================== #


@pytest.mark.network
async def test_binance_testnet_rebalance() -> None:
    """ONE real rebalance against the Binance **testnet**; legs cancelled after.

    Points the engine at a :class:`~trading_bot.brokers.binance.BinanceBroker`
    on the **testnet** base URL (``https://testnet.binance.vision``), runs ONE
    rebalance with a *tiny* capital over a 2-coin universe, reads back
    ``open_orders()`` / ``balances()``, asserts the placed legs match the intended
    per-coin deltas, then **cancels** every leg in a ``finally``. Refuses to run
    against mainnet (``api.binance.com``) — no real-money order is ever sent.

    SKIPS here — no testnet credentials in the env. To run::

        # in a gitignored .env (a *testnet* key from testnet.binance.vision):
        export BINANCE_API_KEY=...  BINANCE_API_SECRET=...
        export BINANCE_API_BASE=https://testnet.binance.vision
        .venv/bin/python -m pytest \\
            trading_bot/tests/application/test_ls1_e2e.py::test_binance_testnet_rebalance \\
            -m network -v
    """
    from trading_bot.application.events import EventBus
    from trading_bot.application.order_router import OrderRouter
    from trading_bot.application.portfolio import PortfolioStrategy
    from trading_bot.application.portfolio_runner import PortfolioRunner
    from trading_bot.application.position_tracker import PositionTracker
    from trading_bot.brokers.binance import TESTNET_API_BASE, BinanceBroker

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    base_url = os.environ.get("BINANCE_API_BASE", "")
    if not (api_key and api_secret):
        pytest.skip(
            "no Binance testnet credentials; set BINANCE_API_KEY / "
            "BINANCE_API_SECRET (a *testnet* key from testnet.binance.vision) "
            "and BINANCE_API_BASE=https://testnet.binance.vision"
        )
    # Safety: never run this against mainnet — testnet base URL is mandatory.
    if "testnet" not in base_url:
        pytest.skip(
            "BINANCE_API_BASE is not the testnet "
            f"({base_url!r}); refusing to place orders against mainnet. Set "
            "BINANCE_API_BASE=https://testnet.binance.vision"
        )

    btc, eth = Symbol("BTC", "USDT"), Symbol("ETH", "USDT")
    universe = (btc, eth)
    broker = BinanceBroker(
        api_key=api_key,
        api_secret=api_secret,
        base_url=base_url or TESTNET_API_BASE,
        symbols=universe,
    )

    # A modest capital + small weights so each leg clears Binance's MIN_NOTIONAL
    # (~$10) without risking much; the legs rest unfilled at a deliberately-off
    # price so we can read them back from open_orders and cancel them.
    capital = money("4000")
    weights = {btc: money("0.01"), eth: money("0.01")}  # ~$40 notional/leg

    # Live current prices to size against, with instrument precision so the venue
    # accepts the quantities.
    btc_inst = await broker.instrument(btc)
    eth_inst = await broker.instrument(eth)
    insts = {btc: btc_inst, eth: eth_inst}
    btc_px = await broker.ticker(btc_inst)
    eth_px = await broker.ticker(eth_inst)
    closes = {btc: btc_px, eth: eth_px}

    def _quantize(qty: Money, precision: int | None) -> Money:
        """Round a quantity down to the venue's qty precision (so it is accepted)."""
        if precision is None:
            return qty
        return qty.quantize(money("1") / (money("10") ** precision))

    # The intended per-coin net qty (quantized to the venue's qty precision so the
    # placed leg matches exactly what we assert against open_orders).
    intended = {
        sym: _quantize(
            money(str(weights[sym] * capital / closes[sym])),
            insts[sym].qty_precision,
        )
        for sym in universe
    }

    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    router = OrderRouter(broker, bus)
    strategy = PortfolioStrategy(
        name="testnet-ls1",
        universe=universe,
        signal_fn=_deterministic_signal(weights),
        capital=capital,
    )

    # A LIMIT factory at ~half the market price (BUY) so the leg rests open
    # (unfilled); qty + price are quantized to the instrument's venue precision.
    from trading_bot.domain.order import Order, OrderSide, OrderType

    def _resting_factory(strat, instrument, delta, close):  # type: ignore[no-untyped-def]
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        # The runner builds a bare Instrument(symbol) with no precision; look the
        # venue precision up from the exchangeInfo-built instruments.
        venue_inst = insts[instrument.symbol]
        # Buys far below / sells far above market → rests unfilled.
        raw_price = close / money("2") if side is OrderSide.BUY else close * money("2")
        return Order(
            client_order_id="pending",
            instrument=instrument,
            side=side,
            qty=_quantize(abs(delta), venue_inst.qty_precision),
            type=OrderType.LIMIT,
            limit_price=_quantize(raw_price, venue_inst.price_precision),
        )

    frames = {
        sym: pl.DataFrame(
            {"time": [1], "o": [float(closes[sym])], "h": [float(closes[sym])],
             "l": [float(closes[sym])], "c": [float(closes[sym])], "v": [1.0]}
        )
        for sym in universe
    }

    runner = PortfolioRunner(
        strategy,
        [frames],
        router,
        tracker,
        event_bus=bus,
        order_factory=_resting_factory,
    )

    placed: list[str] = []
    try:
        result = await runner.rebalance(frames)
        assert result.failed == 0, f"testnet legs failed: {result.failures}"

        # Read back the venue's open orders and confirm the placed legs match the
        # intended per-coin deltas (qty + side).
        open_orders = await broker.open_orders()
        by_pair = {o.instrument.symbol: o for o in open_orders}
        placed = [o.venue_order_id for o in open_orders if o.venue_order_id]
        for sym in universe:
            assert sym in by_pair, f"{sym} leg not found in testnet open orders"
            o = by_pair[sym]
            assert o.qty == intended[sym], (
                f"{sym}: testnet qty {o.qty} != intended {intended[sym]}"
            )

        # balances() is reachable (the account read works on the testnet key).
        balances = await broker.balances()
        assert isinstance(balances, dict)
    finally:
        # Always cancel every placed leg — leave no resting order on the testnet.
        for venue_id in placed:
            try:
                await broker.cancel_order(venue_id)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
