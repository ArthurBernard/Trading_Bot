# 02 — Architecture

Hexagonal, async-first, mirroring dccd's layering under the same `trading_bot/`
package. All layers are shipped; the one maintainer step left (real-key live
enablement) is tracked in [`07-roadmap.md`](07-roadmap.md).

```
trading_bot/
  domain/        # pure, sync, zero I/O
  transport/     # async I/O primitives
  brokers/       # exchange adapters behind a Broker port
  storage/       # persistence + reconciliation source
  application/   # the engine (use-cases, wiring)
  interfaces/    # CLI + FastAPI/Jinja2 control-plane dashboard
  tests/
```

## Domain layer (`domain/`)

Pure, synchronous, no I/O. Never imports transport/brokers/storage.

| Module | Contents |
|--------|----------|
| `instrument.py` | `Instrument` / `Symbol` — venue-neutral pair identity, normalisation |
| `money.py` | `Money` / quantity helpers — **`Decimal` everywhere**, never float |
| `order.py` | `Order` + lifecycle **state machine** (new → submitted → open → partially-filled → filled / cancelled / rejected); order types (market, limit, stop-loss, best-limit) |
| `fill.py` | `Fill` — a broker-confirmed execution; the source of truth for PnL |
| `position.py` | `Position` — net exposure per instrument from fills |
| `signal.py` | `Signal` — a strategy's target (direction / target position) |
| `performance.py` | pure PnL/KPI computations (delegates to fynance where useful) |
| `errors.py` | `OrderError`, `InsufficientFunds`, `RiskLimitBreached`, … |

## Transport layer (`transport/`)

Async only. Drives I/O; domain stays pure. Mirrors dccd's transport.

| Module | Contents |
|--------|----------|
| `http.py` | `AsyncHTTPClient` — httpx wrapper with retry/backoff |
| `ws.py` | `WebSocketBase` — `stream_raw()` async generator with exponential reconnect |
| `ratelimit.py` | `RateLimiter` — token-bucket per exchange (Kraken call-counter) |

## Brokers (`brokers/`)

One class per exchange implementing the **`Broker` port**: place/cancel/replace
orders, fetch open orders, balances, fills, and (where used) market data. Adapters
declare capabilities; multi-exchange is designed for from day one.

| Broker | Status |
|--------|--------|
| `kraken.py` + `kraken_ws.py` | **shipped** — REST + public WS + private executions/fills WS |
| `binance.py` | **shipped** — spot REST (HMAC-SHA256, testnet-capable) |
| `paper.py` | **`PaperBroker`** — in-process simulation behind the same port (default) |
| others (Bitfinex, …) | declared, raise early until implemented |

**Adding an exchange**: add the adapter here, wire it in
`application/service_factory.py`. See [`04-brokers.md`](04-brokers.md) for the
capability matrix.

## Storage (`storage/`)

Append-only order/fill history + engine state (SQLite). This is the
**reconciliation source**: on startup / reconnect the engine compares local state
with what the broker reports and converges.

## Application (`application/`)

| Module | Contents |
|--------|----------|
| `config.py` | `AppConfig` (pydantic) — strategies, brokers, data sources, risk limits |
| `events.py` | `EventBus` — pub/sub fan-out (orders, fills, PnL, logs) |
| `strategy.py` / `strategy_runner.py` | `Strategy` (config + fynance signal, safe loader) and the live loop: data → signal → target position → orders via the router |
| `portfolio.py` / `portfolio_runner.py` | multi-asset analogues — a `PortfolioStrategy` driving a universe from a weight vector, and its runner |
| `data_feed.py` / `data_provider.py` / `portfolio_feed.py` | causal, freshness-gated bar windows fed from dccd (single- and multi-asset) |
| `order_router.py` | idempotent submit (client-order-id), routing, broker-response → domain Order |
| `reconcile.py` | startup/reconnect: refetch open orders+balances+fills, converge local state |
| `position_tracker.py` | net positions from broker-confirmed fills |
| `live_fills.py` | `LiveFillStreamer` — pump a venue's live fills onto the engine bus |
| `performance_service.py` / `pnl_series.py` | live PnL/KPI service (KPI via fynance) + realised-PnL / equity curve helpers |
| `risk.py` | `RiskManager` — pre-trade limits (max order/position/daily-loss) + **kill-switch** |
| `orchestrator.py` / `supervisor.py` | run many strategy loops concurrently (`Orchestrator`) with each declared strategy as an independent supervised unit |
| `run_app.py` | the triptych entrypoint — one `AppConfig` runs the whole system |
| `service_factory.py` | **single wiring point** — builds brokers, stores, feeds (per-venue dispatch, no registry) |

## Interfaces (`interfaces/`)

- `cli/` — Typer `trading-bot` CLI: `run` (run the declared system / demo),
  `start` (supervise strategies as a daemon, step on a schedule), `status`,
  `kpi`, `dashboard`, `serve`, `version`. Replaces the pre-2026 `blessed` CLI
  **and** the multiprocessing server (async orchestration instead of
  processes-over-socket).
- `api/` (FastAPI) + `ui/` (Jinja2, `templates/` + `static/`) — the unified
  **control-plane dashboard** (Overview / Strategies / Orders / PnL / Logs). It is
  **not** read-only: it exposes control routes (deploy a strategy, set an engine
  `mode`, start/stop). The one deliberate exception is orders — there is **no POST
  order route** (a POST to a plausible order path returns `405`), so a compromised
  web client cannot place an order. `trading-bot serve` is a **read-only** alias of
  `dashboard --read-only` for a view-only deployment.

## Data flow

```
dccd (prices) ─▶ StrategyRunner ─▶ Signal ─▶ target Position
                                                  │
                                          OrderRouter (idempotent)
                                                  │  ── RiskManager gate ──
                                                  ▼
                                     Broker (paper | kraken | binance)
                                                  │  fills
                                                  ▼
                              PositionTracker / PerformanceService / storage
```
