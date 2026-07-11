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

## Road to v1.0.0

**v1.0.0 means: the engine trades real money safely, and the public contract
(YAML config schema, CLI, HTTP API) freezes.** The engine is feature-complete
(`06-status.md`); what separates 0.11.x from 1.0 is the live bridge, its safety
companions, operational readiness, and one strategy decision. In dependency
order — none started yet, each is a `/pick-task` candidate:

0. **Decision first — the first live strategy** (maintainer choice, gates the
   scope of everything below):
   - **long-only / spot** → shortest path: the existing Kraken/Binance spot
     adapters suffice; the futures adapter moves post-1.0;
   - **LS1 (long/short)** → requires the **Binance USDT-M futures adapter**
     (testnet `testnet.binancefuture.com` for a faithful live-test *and*
     mainnet for the real book — a full epic; the spot testnet cannot short, a
     spot "live test" of LS1 would silently drop every short leg).
   Kraken has no testnet either way: real-key validation happens with **small
   real sums + tight risk limits**.

1. [ ] **Real-key live enablement — THE gate.** Validate with a real key what
   the offline suite cannot prove (`09-go-live.md`, "Proven vs pending"):
   **venue-level order idempotency** (the venue honouring the client-order-id,
   closing the crash-gap double-submit — the one open money risk), a **real
   cancel** (the kill-switch path against the live API), and real network-edge
   behaviour of **ambiguous submits** (timeout → unknown outcome). Private
   reads are already validated read-only on mainnet, both venues. Then flip
   `live_enabled`.

2. [ ] **Live funds gate** (strategy-capital deferral — lands WITH #1; live
   capital ops currently return 409). Two layers: supervisor-level admission on
   deposit (Σ live allocations on a venue ≤ real venue balance) + an **async**
   available-funds pre-check in `OrderRouter._do_submit` before
   `RiskManager.check` (a sync `funds_provider` inside the risk manager cannot
   await `broker.balances()`). Paper/testnet stay unconstrained by design.

3. [ ] **Operational safety surface** (must exist before a live order, UI-side):
   order-cancel endpoint (`OrderRouter.cancel()` exists, no route — the Orders
   page can only observe); kill-switch trip/reset + status (`tripped`/reason +
   risk-limit visibility incl. daily-loss usage); `last_error` on
   `/api/strategies` (a stopped unit is indistinguishable from a crashed one).

4. [ ] **Ops readiness** (not engine code): daemon under **systemd** (restart
   policy + an alert when the process dies — today it is a `nohup` from a
   terminal session); a **multi-week paper soak** on the real-data books
   (running since 2026-07-10, capital 100/strategy — KPIs and equity curves as
   evidence); **backup of the trading stores** (`var/dashboard/*.sqlite` — the
   dccd data has its hourly rclone sync, the books have nothing).

5. [ ] **API contract freeze** (last, before tagging): pagination beyond the
   200-row tail + server timestamps on snapshot endpoints, then the public
   contract is declared stable — 0.x-style breaking removals end at 1.0.

## UI consistency & accounting integrity (2026-07-11)

A dashboard-driven audit (position ≠ Σ orders on `alloc1-binance`) uncovered two
engine bugs and a set of API/UX gaps. Shipped 2026-07-11 (plan trees archived):
the `paper-integrity` epic (durable paper ids, order↔fill lifecycle sync,
persisted orphan-closes — PRs #190–#194, v0.12.0; restart the daemon to heal
the live books) and the `accounting-guardrail` epic (pure invariant checker,
startup + 60 s-TTL wiring with SSE alerts, per-strategy health surface incl.
the kill-switch fold — the *status* half of road-to-1.0 #3 — and the dashboard
health pill; PRs #197–#201). In dependency order:

3. [ ] **`venue-minimums` — venue minimums end-to-end.** Flip the dashboard
   wiring to `PaperBroker(strict=True)`; add the order-prep policy in the
   portfolio runner (round up to venue minimum when close, skip + log when far
   below — never fail the whole rebalance; the next rebalance recomputes the
   residual naturally).
4. [ ] **`api-completeness` — API exposes what the app layer knows.** Per-
   position mark price (v1 = last dccd bar close, always with its `asof` ts;
   live ticker is post-1.0), value + unrealised PnL per position; display
   currency (global default + per-exchange override, static conversion rates);
   `/api/balances`; `fee_ccy`; `last_asof_ts` on every surface. Must land
   before the API contract freeze (road-to-1.0 #5).
5. [ ] **`dashboard-tables-ux` — tables redesign.** Positions keyed by asset
   (not pair) with value/mark/unrealised as primary columns; expandable rows
   for secondary detail (fills under their order, ids, fee breakdown —
   expanded state survives the SSE re-render); fills demoted to order detail
   (flat audit view stays on the Orders page); "last bar → next bar" timing
   chip; timezone affordance.
6. [ ] **`canary-roundtrip` — deterministic self-test strategy.** A minimal
   round-trip canary run as its own strategy unit with its own tiny ledger
   (~10 USDT): **sequential** market buy *x* then sell *x* (never simultaneous
   — self-trade prevention would make it non-deterministic), plus two free
   probes: a far-off-limit **cancel** (the real kill-switch path) and a
   client-order-id **idempotency** re-submit. Two oracles: **paper** = exact
   Decimal equality against the precomputed expectation (PnL = −2·fees, flat
   position, terminal orders — CI-runnable, per release); **live** = exact
   *accounting identity* (our fills/balances == venue-reported) + **bounded**
   total cost (≤ 1–2 EUR) — never a precomputed absolute PnL (spread/drift are
   not deterministic). Cadence: paper per release; testnet at will; live once
   per venue at go-live + after any broker-adapter change. Depends on #1
   (order lifecycle) + #2 (health surface for the pass/fail report) + #3
   (venue minimums; Binance BNB-fee discount must be off or modeled). The live
   canary is the validation vehicle for **Road to v1.0.0 #1** (real-key
   enablement: venue idempotency, real cancel, balance reconciliation).

## Not gating 1.0 (post-1.0 candidates)

- [ ] **Binance USDT-M futures adapter** — *unless* chosen as the first live
  path in the decision above (then it joins the road to 1.0). Testnet
  (`testnet.binancefuture.com`, supports shorts) + mainnet; prerequisite for a
  faithful testnet live-test of any long/short book. Until then, long/short
  strategies stay **paper**; spot/long-only strategies can use the spot testnet
  today.
- [ ] **Dashboard follow-ups (strategy-capital deferrals).** Overview portfolio
  money band (capital deployed / total value / withdrawable) + allocation
  breakdown; `close-and-refund` teardown (flatten + drain the ledger — with its
  **own** cancel path, never overloading the kill-switch semantics) + a
  `remove_unit` flat-and-zero guard; state polish (error/stale pills, live red
  banner on the detail page, one unified SSE+poll refresh model).

> **Live fill streaming — done.** The private `KrakenPrivateWS` is wired into the run
> loop via `LiveFillStreamer` (real-money live Kraken only), reconcile fires on every
> WS (re)connect, and fills are de-duplicated by id. Validated **read-only** against
> real Kraken (no order sent). See `03-decisions.md`.

> **Project name — decided:** kept as `trading_bot` (with `dccd` / `fynance`). The
> deferred "final name" decision is **closed**; no rename. See `03-decisions.md`.
