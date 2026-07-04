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

## Audit remediation (2026-07-02)

Full deep audit landed in [`audit/`](audit/) — 90 findings (4 Critical, 16 High,
30 Medium, 24 Low, 16 Info). All money-risk findings are **latent** (paper/testnet
only today); they gate go-live.

**Wave 1 (all 4 Critical + top High) shipped in v0.8.0** — test-gate hermeticity,
domain money guards, transport secret redaction, broker live-readiness, dashboard
gate hardening, engine risk + storage, docs groom (see `CHANGELOG.md`; plan tree
archived).

**Wave 2 (the "needs care" items) shipped in v0.9.0** — SQLite writes off the event
loop + WAL/busy_timeout + fill-id mode isolation (`A-2`/`D-8`/`D-9`), per-unit
supervisor lock (`A-3`/`A-10`), weight-aware Binance limiter (`B-6`/`B-9`/`B-7`),
true avg fill price + Kraken WS reconcile-on-gap (`B-8`/`B-10`) (see `CHANGELOG.md`;
plan tree archived).

**Wave 3 (the long tail — remaining Medium/Low) shipped in v0.10.0** — dashboard
web-surface hardening (`I-4`…`I-13`, `A-4`), engine/order robustness
(`A-6`…`A-9`), transport hardening + strict PaperBroker (`B-11`…`B-13`), domain
hygiene (`D-6`/`D-11`/`D-12`/`D-13`/`A-12`), tooling/CI/packaging parity
(`T-5`…`T-11`, `G-13`), and money-critical error-branch coverage + dead-code
removal (`T-4`/`T-12`/`I-12`/`A-11`/`G-14`) (see `CHANGELOG.md`). **The audit is
fully remediated** — only pure Info observations remain in `doc/dev/audit/`.

## Known issues / follow-ups

- [ ] **Dashboard UX overhaul** — readable numbers (display rounding, units,
  currency), visible evaluation cadence (next tick / next bar close), positions
  grouped by strategy, table/feed polish. Plan tree:
  [`plans/ui-ux-overhaul/`](plans/ui-ux-overhaul/00-plan.md).
- [ ] **Binance futures/margin testnet adapter (for a faithful long/short testnet
  live-test).** The `BinanceBroker` is **spot** (`/api/v3`), and the Binance testnet
  it reaches (`testnet.binance.vision`) is spot-only — it **cannot short**.
  Long/short portfolio strategies (typically net-short) therefore can
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
