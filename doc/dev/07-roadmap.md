# 07 — Roadmap

The single source *index* of open work. Each unchecked item is a candidate for
`/pick-task` → `/plan` (which expands it into a `plans/<epic>/` tree) →
`/execute-leaf` → `/finish-task`. History of what shipped stays in git + CHANGELOG.

> Order is roughly sequential (E1 → E10); dependencies noted inline. Re-slice
> freely — an epic may ship as several small PRs.
>
> **Full decomposition** — every epic broken into its leaves, branches,
> complexity and dependencies: [`08-program-plan.md`](08-program-plan.md).

## Foundation

- [ ] **E1 — Domain core.** `Order` (+ lifecycle state machine), `Position`,
  `Fill`, `Signal`, `Instrument`, `Money` (Decimal), pure PnL/KPI, errors. Pure,
  typed strict, unit-tested. Mine `legacy/orders.py` + `legacy/performance.py` for
  spec.
- [ ] **E2 — Transport.** `AsyncHTTPClient` (httpx + retry/backoff),
  `WebSocketBase` (reconnect), `RateLimiter` (token-bucket / Kraken call-counter).
  Mirror dccd's transport. _(depends on E1 for typing only)_
- [ ] **E3 — Broker port + Kraken adapter.** `Broker` protocol (place/cancel/
  replace, open orders, balances, fills, market data) + registry; `KrakenBroker`
  (REST first, WS fills next). Other venues declared, not implemented.
  _(depends on E1, E2)_

## Execution engine

- [ ] **E4 — Order router + PaperBroker.** Idempotent submit (client-order-id),
  routing, reconciliation; `PaperBroker` simulation behind the same port;
  `PositionTracker` from confirmed fills. _(depends on E3)_
- [ ] **E5 — Strategy runner.** Load a strategy (config + fynance signal), feed it
  data from dccd, emit target positions/orders; the live loop. Replaces
  `legacy/StrategyBot`. _(depends on E4)_
- [ ] **E6 — Performance, persistence & risk.** `PerformanceService` (PnL/KPI),
  `storage` (SQLite order/fill history + state for reconciliation), `RiskManager`
  (pre-trade limits + kill-switch). _(depends on E4)_

## Interfaces & orchestration

- [ ] **E7 — CLI.** Typer CLI (start/stop strategies, status, KPI table) + async
  orchestration replacing the legacy multiprocessing server. Declare the
  `trading-bot` console script. Delete superseded legacy modules. _(depends on E5, E6)_
- [ ] **E8 — Orchestration of the triptych.** One `AppConfig` declaring data
  sources (dccd) + strategies (fynance) + brokers; a single entrypoint that wires
  the three. Decide library-import vs service-driving for dccd here. _(depends on E7)_

## Later

- [ ] **E9 — Web UI.** FastAPI + Jinja2 dashboard (positions/orders/PnL),
  mirroring dccd's UI. _(depends on E8)_
- [ ] **E10 — Go-live hardening & final name.** Live-trading checklist
  (reconciliation, kill-switch, idempotency proven under fault injection); choose
  and apply the final project name. _(depends on E8)_
