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

## Active epics (post-0.2.0)

- [ ] **E11 — Binance adapter (2nd live venue).** `BinanceBroker` REST behind the `Broker` port (HMAC-SHA256 signing vs Binance's vector; orders/balances/fills/ticker; `newClientOrderId` idempotency; testnet-capable), wired into `service_factory`. Public market data key-free; private path proven by mocks + an opt-in Binance **testnet** E2E. WS deferred.
- [ ] **Multi-asset / portfolio strategy unit** (signalé par `fynance-research`). Today a
  `SignalFn` is `(bars) -> Signal` for **one** instrument; the first validated research
  strategy, **LS1**, is a **multi-asset long/short book** (a *vector* of target weights over
  ~10 Binance USDT coins, gross-capped 2×). Add a portfolio-strategy abstraction that consumes
  a weight vector in one shot (e.g. `fynance_research.strategies.ls1_live.target_weights()` →
  `{pair: weight}`, fraction of capital, Σ\|w\|≤2) and emits N venue-neutral `Signal`s + the
  per-coin order routing. **Interim**: deployable now as 10 single-instrument strategies each
  calling `ls1_live.coin_exposure("<PAIR>")` (works, but recomputes the book 10×). Spec:
  `fynance-research/DEPLOY_LS1.md`. Daily rebalance; reads bars from the dccd Binance store.

## Open / deferred (maintainer decisions)

- [ ] **Final project name.** Kept `trading_bot` for now — choose and apply a final
  name when ready (touches the package/repo/docs).
- [ ] **Real-key live enablement.** Validate Kraken private endpoints + venue-level
  order idempotency against a **real-key sandbox**, then flip `live_enabled` — the one
  remaining prerequisite before real-money trading. See `doc/dev/09-go-live.md`.
