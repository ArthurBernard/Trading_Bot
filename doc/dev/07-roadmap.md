# 07 — Roadmap

The single source *index* of open work. Each unchecked item is a candidate for
`/pick-task` → `/plan` (which expands it into a `plans/<epic>/` tree) →
`/execute-leaf` → `/finish-task`. History of what shipped stays in git + CHANGELOG.

> Order is roughly sequential (E1 → E10); dependencies noted inline. Re-slice
> freely — an epic may ship as several small PRs.
>
> **Full decomposition** — every epic broken into its leaves, branches,
> complexity and dependencies: [`08-program-plan.md`](08-program-plan.md).

**The E1–E10 rewrite is complete.** The hexagonal engine conducts the triptych
(dccd data + fynance signals + brokers) via the `trading-bot` CLI and a read-only web
dashboard; paper-by-default, hardened under fault injection, live behind an explicit
off-by-default opt-in. History in git + `CHANGELOG.md`; see `06-status.md`.

**Post-0.2.0 shipped:** the **Binance adapter** (E11, 2nd live venue) and the
**native multi-asset / portfolio-strategy unit** (LS1 runnable by config —
`configs/ls1.yaml`, real dccd-data verified). History in `CHANGELOG.md`.

## Known issues / follow-ups

- [ ] **Portfolio config → real-dccd store-key convention unpinned.** The default
  `PortfolioFeed` renders pairs via `to_venue_symbol(exchange)` (`BTCUSDT`; Kraken
  `XBTUSD`/`TRXUSD`), which (a) doesn't match a hyphen-keyed `BASE-QUOTE` dccd store and
  (b) is ambiguous to invert (`XBTUSD`→`XB/TUSD`, `TRXUSD`→`TR/USD` under the parsers).
  The LS1 tests' fake dccd client normalises by existence-checking the store; a real
  `trading-bot run configs/ls1*.yaml` against a live `dccd.Client` needs the convention
  pinned (a `symbol_for`/store-key field on `PortfolioStrategyConfig`, or a canonical
  render). Until then LS1 runs are verified via the test harness, not raw `run_app`.
- [ ] **PaperBroker/engine drain is superlinear (~O(n²)) over accumulated ticks.** A
  full multi-year daily run through `run_app` is slow (≈10 ticks 6.5s → 200 ticks 118s);
  the LS1 real-data tests assert on a single latest-cross-section rebalance instead.
  Profile the per-fill rebuild (tracker/perf/bus) and make the drain linear before any
  long backtest/live-replay through the engine.
- [ ] **dccd API drift breaks two `-m network` data-feed tests.** `dccd.Client().inventory()`
  is now called outside an `async with` in `test_data_feed.py` / `test_data_provider.py`
  (current dccd rejects it). Network-only (not in CI). Realign the dccd client usage.

## Open / deferred (maintainer decisions)

- [ ] **Final project name.** Kept `trading_bot` for now — choose and apply a final
  name when ready (touches the package/repo/docs).
- [ ] **Real-key live enablement.** Validate Kraken private endpoints + venue-level
  order idempotency against a **real-key sandbox**, then flip `live_enabled` — the one
  remaining prerequisite before real-money trading. See `doc/dev/09-go-live.md`.
