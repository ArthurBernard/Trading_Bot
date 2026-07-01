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

## Active epics

- [ ] **Unified dccd-style dashboard.** One FastAPI app (monitor + control), one
  `dashboard` command (clean Ctrl-C + systemd), a shared `base.html` shell, SSE live,
  **KPI per strategy/exchange/total**, groupable positions/orders, and a **per-strategy
  PnL chart (uPlot, live vs testnet separate)** — replacing the split read-only/control
  apps. Plan: `plans/unified-dashboard/` (6 leaves).

## Known issues / follow-ups

- [ ] **Binance futures/margin testnet adapter (for a faithful long/short testnet
  live-test).** The `BinanceBroker` is **spot** (`/api/v3`), and the Binance testnet
  it reaches (`testnet.binance.vision`) is spot-only — it **cannot short**.
  Long/short portfolio strategies (e.g. ALLOC1, typically net-short) therefore can
  only be *paper*-tested faithfully; a testnet "live test" would silently drop every
  short leg. A USDT-M **futures** testnet adapter (`testnet.binancefuture.com`, which
  supports shorts) is the prerequisite for a faithful testnet live-test of a
  long/short book. Until then, long/short strategies stay **paper**; spot-only or
  long-only strategies can use the spot testnet today.

> **Live fill streaming — done.** The private `KrakenPrivateWS` is wired into the run
> loop via `LiveFillStreamer` (real-money live Kraken only), reconcile fires on every
> WS (re)connect, and fills are de-duplicated by id. Validated **read-only** against
> real Kraken (no order sent). See `03-decisions.md`.

## Open / deferred (maintainer decisions)

- [ ] **Real-key live enablement.** Validate Kraken private endpoints + venue-level
  order idempotency against a **real-key sandbox**, then flip `live_enabled` — the one
  remaining prerequisite before real-money trading. See `doc/dev/09-go-live.md`.

> **Project name — decided:** kept as `trading_bot` (with `dccd` / `fynance`). The
> deferred "final name" decision is **closed**; no rename. See `03-decisions.md`.
