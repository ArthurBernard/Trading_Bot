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
**native multi-asset / portfolio-strategy unit** (strategies run by config via the
generic `as_portfolio_signal` adapter; concrete strategies kept **local-only** under
the gitignored `strategies/`, real dccd-data verified). History in `CHANGELOG.md`.

## Known issues / follow-ups

- [ ] **Live fill streaming + post-disconnect reconcile.** Reconcile is now wired
  **on startup** (`run_app` converges the engine to the venue before the first order),
  but the **after-disconnect** half of *reconcile, don't assume* is not: the private
  fill WS (`KrakenPrivateWS`) is not wired into the run loop, so there is no reconnect
  to trigger a reconcile pass on, and live fills are not streamed onto the bus. Wire
  the private fill WS into the engine and trigger `reconcile` on each reconnect (and
  land fill-id dedup — see below — before that stream feeds the tracker).
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
