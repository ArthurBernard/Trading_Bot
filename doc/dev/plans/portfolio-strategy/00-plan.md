---
plan: portfolio-strategy
kind: global
status: planning
roadmap: "- [ ] **Multi-asset / portfolio strategy unit** (signalé par `fynance-research`). Today a `SignalFn` is `(bars) -> Signal` for **one** instrument; the first validated research strategy, **LS1**, is a **multi-asset long/short book** (a *vector* of target weights over ~10 Binance USDT coins, gross-capped 2×). Add a portfolio-strategy abstraction that consumes a weight vector in one shot (e.g. `fynance_research.strategies.ls1_live.target_weights()` → `{pair: weight}`, fraction of capital, Σ\\|w\\|≤2) and emits N venue-neutral `Signal`s + the per-coin order routing. **Interim**: deployable now as 10 single-instrument strategies each calling `ls1_live.coin_exposure(\"<PAIR>\")` (works, but recomputes the book 10×). Spec: `fynance-research/DEPLOY_LS1.md`. Daily rebalance; reads bars from the dccd Binance store."
release_on_done: false
---

# Multi-asset / portfolio strategy unit (native)

## Goal

Add a **native portfolio-strategy** abstraction to `trading_bot`: a *single*
logical strategy that spans **N instruments**, consumes a **target-weight vector**
`{Symbol: weight}` (weight = signed fraction of capital, Σ|w| ≤ cap) **in one
shot**, and drives all N instruments to target via idempotent, risk-gated orders
on one venue. The validating use case is **LS1** (`../fynance-research`,
`DEPLOY_LS1.md`): a daily long/short book over **10 Binance USDT** coins
(`BTC ETH BCH LTC XRP XLM DOGE DOT TRX ZEC`), Σ|w| ≤ 2, bars from **dccd's Binance
store**, daily rebalance.

This is the **clean** path the LS1 dossier calls for (vs the interim "10
single-instrument strategies each calling `coin_exposure()`" — which works today
but recomputes the book 10× and fights `_reject_commingled`). `trading_bot` stays
**generic**: the portfolio signal is loaded **by reference**
(`module:function`, exactly like the single-instrument `SignalFn`), so LS1 lives in
config (`fynance_research.strategies.ls1_live:target_weights`), not in the engine.

## Why now

E11 just landed **Binance** as the execution venue. LS1 is the first validated
research strategy and it is *inherently multi-asset*, so the single-instrument
`SignalFn`/`StrategyRunner` cannot express it. This epic is the missing execution
shape. It is **0.3.0 work** (post the 0.2.0 release).

## Context the leaves rely on (already in the repo)

- `domain.signal.Signal.target_qty(instrument, qty)` — an **explicit signed net
  quantity** signal (no `[-1,1]` bound), exactly right for `weight × capital /
  price` (a single coin's weight can exceed 1 under leverage). `delta_to(position)`
  gives the order size/side.
- `application.run_app.build_runners` / `_resolve_signal_fn` / `_reject_commingled`
  — the single-instrument wiring to mirror/extend; `StrategyRunner` — the loop to
  mirror; `application.data_provider.feed_for` + `DataSourceConfig` — the dccd feed
  to generalise to N coins; the shared `Engine` (`OrderRouter`+risk, `PositionTracker`,
  `EventBus`, `PerformanceService`).
- `brokers.BinanceBroker` (E11) — the venue; maker LIMIT orders for the 0.10% fee.

## Decomposition

1. **portfolio-signal** — the `PortfolioSignalFn` contract + a safe by-reference
   loader + the weight→`Signal.target_qty` sizing helper. (application)
2. **portfolio-feed** — a multi-instrument, common-date-index daily-bars feed from
   dccd's Binance store, with an "all N coins have today's close" freshness gate.
3. **portfolio-runner** — the rebalance loop: weights × capital ÷ price → per-coin
   `delta_to(position)` → N idempotent risk-gated **maker LIMIT** orders; hold to
   the next (daily) tick.
4. **portfolio-config** — `PortfolioStrategyConfig` in `AppConfig` + wiring into
   `run_app`/`build_runners`; `fynance-research` as an optional editable extra.
5. **ls1-e2e** — LS1 end-to-end on real dccd Binance bars (deltas == intended) +
   an opt-in Binance **testnet** rebalance round-trip.

## Leaf checklist

- [x] 01 portfolio-signal — feat/portfolio-signal — medium (→ opus)
- [ ] 02 portfolio-feed — feat/portfolio-feed — medium (→ opus) (depends on 01)
- [ ] 03 portfolio-runner — feat/portfolio-runner — high (→ opus) (depends on 01)
- [ ] 04 portfolio-config — feat/portfolio-config — medium (→ opus) (depends on 02, 03)
- [ ] 05 ls1-e2e — feat/portfolio-ls1-e2e — high (→ opus) (depends on 04)

## Dependencies

- 01 first. 02 and 03 both depend on 01 (and may proceed in parallel, but the
  default is serial). 04 depends on 02 + 03. 05 depends on 04. Otherwise serial.

## Done criteria

- A `PortfolioStrategy` runs end-to-end: one config declares a universe + a
  by-reference weight-vector signal + capital + (daily) cadence + venue=binance;
  `run_app` builds it; it emits the right **N** orders to converge each coin to
  `weight × capital / price`, idempotently, risk-gated, on the shared engine.
- LS1 is wired purely by config (`fynance_research.strategies.ls1_live:target_weights`)
  — no LS1 specifics in the engine. `fynance-research` is an optional `[triptych]`
  extra (offline tests run without it via a fake weight-vector signal).
- **Verified on real data:** over real dccd Binance daily bars, the routed per-coin
  deltas equal `weightᵢ × capital / priceᵢ − current_qtyᵢ`; an opt-in Binance
  **testnet** rebalance places/reconciles the legs against broker-reported state.
- Paper stays the default; the portfolio path is risk-gated (gross cap honoured)
  and money stays `Decimal`. `ruff`/`mypy`/`pytest` green via `.venv`.
- Last leaf (05) removes the multi-asset roadmap line from `07-roadmap.md` and
  updates `06-status.md`.
