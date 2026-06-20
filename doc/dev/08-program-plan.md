# 08 — Program plan (E1 → E10)

The **complete map** of the rewrite: every epic decomposed into its leaves, each
leaf with its **branch**, **complexity**, **dependencies** and one-line intent.
This removes grey zones about *direction and structure* up front. The precise,
file-by-file **executable specs** for each leaf are written **epic by epic, just
before that epic is executed** (via `/plan`), because the late epics depend on what
the early ones produce — over-specifying them now would be speculation.

> Relationship to the other docs: [`07-roadmap.md`](07-roadmap.md) is the terse
> `- [ ]` index the skills read; **this file** is its full decomposition;
> [`plans/<epic>/`](plans/) holds the executable trees (E1 = `plans/domain-core/`
> already written). [`02-architecture.md`](02-architecture.md) is the layer map
> these epics build out.

## Legend

- **Complexity → execution model**: `low → haiku`, `medium → sonnet`, `high → opus`.
- **Branch types**: `feat/` (new code), `chore/` (tooling/cleanup), `test/` (test-only),
  `docs/`. One leaf = one disposable PR.
- **Deps** are leaf numbers within the epic unless prefixed by an epic (e.g. `E3-02`)
  or an external repo (e.g. `dccd`).

## Epic sequencing & dependency graph

```
E1 domain ─▶ E2 transport ─▶ E3 brokers ─▶ E4 engine ─┬─▶ E5 strategy ─┐
                                                       └─▶ E6 perf/risk ─┴─▶ E7 CLI ─▶ E8 orchestration ─┬─▶ E9 UI
                                                                                                          └─▶ E10 go-live
```

- E5 also needs **dccd** (data) and E2 (transport).
- **Parallelism**: E2's three leaves are independent; **E5 and E6 run in parallel**
  after E4; **E9 and E10 run in parallel** after E8. Everything else is serial.
- The MVP "first light" milestone is reached at **end of E7** (a CLI can run a
  strategy on Kraken in paper mode, with risk + reconciliation). E8 makes it the
  triptych orchestrator; E9 adds the dashboard; E10 hardens for real money.

## Executable-tree status

| Epic | Tree written? |
|------|---------------|
| E1 domain-core | ✅ `plans/domain-core/` (PR #4) |
| E2 … E10 | ⏳ written via `/plan` at the start of each epic |

---

## E1 — Domain core  ·  `plans/domain-core/`  ·  **tree written**

Pure, zero-I/O, mypy-strict vocabulary. (Full specs in the tree.)

| Leaf | Branch | Cx | Deps | Intent |
|------|--------|----|------|--------|
| 01 primitives | feat/domain-primitives | medium | — | Money(Decimal), Instrument/Symbol (+Kraken normalisation), errors |
| 02 order | feat/domain-order | high | 01 | Order + lifecycle state machine + order types |
| 03 fill-position | feat/domain-fill-position | medium | 02 | Fill (PnL truth) + Position.from_fills (flips) |
| 04 signal | feat/domain-signal | low | 01 | Signal (venue-neutral target) + delta-to-position |
| 05 performance | feat/domain-performance | high | 03 | Pure PnL/KPI; KPI delegated to fynance |

## E2 — Transport  ·  epic-deps: E1

Async I/O primitives, mirroring dccd's transport. The three leaves are independent
(parallelisable).

| Leaf | Branch | Cx | Deps | Intent |
|------|--------|----|------|--------|
| 01 http | feat/transport-http | medium | E1 | `AsyncHTTPClient` (httpx) — retry/backoff, timeouts |
| 02 ws | feat/transport-ws | medium | E1 | `WebSocketBase.stream_raw()` — exponential reconnect |
| 03 ratelimit | feat/transport-ratelimit | medium | E1 | `RateLimiter` token-bucket + Kraken call-counter model |

## E3 — Broker port + Kraken  ·  epic-deps: E2

The central exchange contract + the one implemented venue.

| Leaf | Branch | Cx | Deps | Intent |
|------|--------|----|------|--------|
| 01 broker-port | feat/broker-port | high | E1, E2 | `Broker` protocol (place/cancel/replace, open orders, balances, fills, market data) + `registry` + capability declaration |
| 02 kraken-rest | feat/broker-kraken-rest | high | 01 | Kraken REST: auth/signing/nonce, place/cancel, open orders, balances, fills; map domain Order ↔ Kraken |
| 03 kraken-ws | feat/broker-kraken-ws | high | 02 | Kraken private WS: own-trades (fills) + order updates |

## E4 — Execution engine  ·  epic-deps: E3

Opens `application/`. Idempotent routing, simulation, position tracking, reconciliation.

| Leaf | Branch | Cx | Deps | Intent |
|------|--------|----|------|--------|
| 01 app-kernel | feat/app-kernel | medium | E1 | `application/config.py` (AppConfig skeleton) + `events.py` (EventBus fan-out) |
| 02 paper-broker | feat/paper-broker | medium | E3-01 | `brokers/paper.py` — in-process fill simulation behind the Broker port (the default) |
| 03 order-router | feat/order-router | high | 01, 02, E3-02 | Idempotent submit (client-order-id), routing, broker-response → domain Order |
| 04 position-tracker | feat/position-tracker | medium | 03 | Net Positions from broker-confirmed fills |
| 05 reconciliation | feat/reconciliation | high | 03, 04 | Startup/reconnect: refetch open orders+balances+fills, converge local state |

## E5 — Strategy runner  ·  epic-deps: E4, E2, **dccd**  ·  parallel with E6

| Leaf | Branch | Cx | Deps | Intent |
|------|--------|----|------|--------|
| 01 strategy-spec | feat/strategy-spec | medium | E1, E4-01 | How a strategy is declared/loaded: config + a fynance-backed signal fn (replaces legacy importlib loading) |
| 02 data-feed | feat/data-feed | high | E2, dccd | Feed adapter consuming dccd (live WS prices + historical bars) |
| 03 strategy-runner | feat/strategy-runner | high | 01, 02, E4-03 | The live loop: data → signal → target position → orders via the router |

## E6 — Performance, persistence & risk  ·  epic-deps: E4  ·  parallel with E5

| Leaf | Branch | Cx | Deps | Intent |
|------|--------|----|------|--------|
| 01 storage | feat/storage | high | E1, E4-03 | SQLite append-only order/fill history + engine state — the reconciliation source |
| 02 performance-service | feat/performance-service | medium | E1-05, E4-04 | Live PnL/KPI service over the fill stream (wraps domain.performance) |
| 03 risk-manager | feat/risk-manager | high | E4-03 | Pre-trade gate (max position/order/daily-loss) + **kill-switch** |

## E7 — CLI & async orchestration  ·  epic-deps: E5, E6  ·  **MVP "first light"**

| Leaf | Branch | Cx | Deps | Intent |
|------|--------|----|------|--------|
| 01 cli-skeleton | feat/cli-skeleton | medium | E5, E6 | Typer app + `service_factory` wiring + `trading-bot` console script |
| 02 cli-commands | feat/cli-commands | medium | 01 | start/stop/status strategies, list, KPI table (legacy `blessed` → Typer/rich) |
| 03 async-orchestration | feat/async-orchestration | high | 01, E5-03 | Async lifecycle of strategy loops — replaces the legacy multiprocessing server |
| 04 legacy-removal | chore/remove-legacy-superseded | low | 02, 03 | Delete the legacy modules now fully replaced; tidy packaging/docs |

## E8 — Orchestration of the triptych  ·  epic-deps: E7

| Leaf | Branch | Cx | Deps | Intent |
|------|--------|----|------|--------|
| 01 app-config-full | feat/app-config | high | E7 | One `AppConfig` (YAML/pydantic) declaring data sources + strategies + brokers + risk |
| 02 dccd-integration | feat/dccd-orchestration | high | 01, E5-02 | **Resolves: library-import vs service-driving for dccd**; wire collection/feed |
| 03 entrypoint | feat/triptych-entrypoint | high | 01, 02 | Single entrypoint wiring the three repos: config → service_factory → run |

## E9 — Web UI  ·  epic-deps: E8  ·  parallel with E10

| Leaf | Branch | Cx | Deps | Intent |
|------|--------|----|------|--------|
| 01 api | feat/web-api | high | E8 | FastAPI: positions/orders/PnL endpoints + SSE for live updates |
| 02 ui | feat/web-ui | high | 01 | Jinja2 dashboard (positions/orders/PnL), pure HTTP client of the API — mirrors dccd |

## E10 — Go-live hardening & final name  ·  epic-deps: E8  ·  parallel with E9

| Leaf | Branch | Cx | Deps | Intent |
|------|--------|----|------|--------|
| 01 fault-injection | test/go-live-hardening | high | E8 | Prove reconnection, idempotency, reconciliation, kill-switch under fault injection |
| 02 live-enablement | feat/live-enablement | high | 01 | **Resolves: paper-vs-live default**; explicit live opt-in + credentials + go-live runbook |
| 03 rename | chore/rename | medium | 02 | **Resolves: final project name**; apply to package/repo/docs (do last) |

---

## Cross-cutting deferred decisions — where each is resolved

| Deferred decision | Resolved at |
|-------------------|-------------|
| fynance (untyped) vs mypy-strict domain | E1-05 (typed wrapper or narrow override) |
| dccd integration depth (library import vs driving a service) | E8-02 |
| paper-vs-live default beyond the MVP | E10-02 |
| final project name | E10-03 |

## Totals

34 leaves across 10 epics. Counts: E1 5 · E2 3 · E3 3 · E4 5 · E5 3 · E6 3 ·
E7 4 · E8 3 · E9 2 · E10 3. This is the structural contract; each epic's executable
tree is written just before it runs, and `/finish-task` ticks the roadmap as leaves
land.
