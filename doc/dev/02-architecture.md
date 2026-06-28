# 02 — Architecture (target)

Hexagonal, async-first, mirroring dccd's layering under the same `trading_bot/`
package. The MVP layers are in place (see [`07-roadmap.md`](07-roadmap.md) for
what comes next).

```
trading_bot/
  domain/        # pure, sync, zero I/O
  transport/     # async I/O primitives
  brokers/       # exchange adapters behind a Broker port
  storage/       # persistence + reconciliation source
  application/   # the engine (use-cases, wiring)
  interfaces/    # CLI, later HTTP/UI
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
| `kraken.py` | **implemented at MVP** (REST + WS) |
| `paper.py` | **`PaperBroker`** — in-process simulation behind the same port (default) |
| others (Bitfinex, …) | declared, raise early until implemented |

**Adding an exchange**: add the adapter here, register it in
`application/service_factory.py`.

## Storage (`storage/`)

Append-only order/fill history + engine state (SQLite). This is the
**reconciliation source**: on startup / reconnect the engine compares local state
with what the broker reports and converges.

## Application (`application/`)

| Module | Contents |
|--------|----------|
| `config.py` | `AppConfig` (pydantic) — strategies, brokers, data sources, risk limits |
| `events.py` | `EventBus` — pub/sub fan-out (orders, fills, PnL, logs) |
| `strategy_runner.py` | loads a strategy (config + fynance signal), feeds it data (dccd), emits target positions/orders |
| `order_router.py` | idempotent submit (client-order-id), routing, **reconciliation** |
| `position_tracker.py` | net positions from broker-confirmed fills |
| `performance.py` | live PnL/KPI service |
| `risk.py` | `RiskManager` — pre-trade limits + **kill-switch** |
| `scheduler.py` | async orchestration of strategy loops |
| `service_factory.py` | **single wiring point** — builds brokers, stores, registries |

## Interfaces (`interfaces/`)

- `cli/` — Typer commands (start/stop strategies, status, KPI table). Replaces the
  pre-2026 `blessed` CLI **and** the multiprocessing server (async orchestration
  instead of processes-over-socket).
- `api/` + `ui/` — FastAPI + Jinja2 dashboard (positions/orders/PnL), later;
  mirrors dccd's UI.

## Data flow (target)

```
dccd (prices) ─▶ StrategyRunner ─▶ Signal ─▶ target Position
                                                  │
                                          OrderRouter (idempotent)
                                                  │  ── RiskManager gate ──
                                                  ▼
                                            Broker (paper | kraken)
                                                  │  fills
                                                  ▼
                              PositionTracker / PerformanceService / storage
```
