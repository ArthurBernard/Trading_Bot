---
plan: web-ui/01-api
kind: leaf
status: planned
complexity: high
depends: []
parallel: false
branch: feat/web-api
pr: ""
---

# Web API — read-only FastAPI over the engine

## Goal

A FastAPI `create_app(engine)` exposing the engine's live state **read-only**:
positions, orders, PnL/KPI as JSON (money as **Decimal strings**), plus an SSE
`/api/events` stream fed by the `EventBus`. The UI (leaf 02) is a pure HTTP client
of this. No endpoint ever places or cancels an order.

## Files to change

- `trading_bot/interfaces/api/__init__.py`, `trading_bot/interfaces/api/app.py` — new; `create_app(engine)`.
- `trading_bot/tests/interfaces/test_api.py` — new.
- `pyproject.toml` — ensure `fastapi`/`uvicorn[standard]` in the `daemon` + `dev` extras.

## Steps

1. Read dccd's FastAPI app for the pattern
   (`/home/arthur/dev/Download_Crypto_Currencies_Data/dccd/interfaces/api/app.py`):
   `create_app()`, module-level pydantic response models, the SSE `/api/events`
   endpoint using `EventBus.add_queue`/`remove_queue`. Read `service_factory.Engine`
   (`tracker`, `perf`, `router`, `bus`), `application/position_tracker.py`
   (`all_positions()`), `application/order_router.py` (`tracked_orders()`),
   `application/performance_service.py` (`realised_pnl`/`fees_paid`/`equity_curve`/KPIs),
   `application/events.py` (`EventBus`, `OrderEvent`/`FillEvent`/`LogEvent`).
2. `create_app(engine) -> FastAPI`: store the engine on `app.state`. Endpoints
   (all **GET**, read-only):
   - `GET /api/health` → `{status, mode, strategies?}`.
   - `GET /api/positions` → list of `{instrument, net_qty, avg_entry_price,
     realised_pnl, fees_paid}` from `engine.tracker.all_positions()`. **All money
     fields are `str(Decimal)`** (use a serializer/response model that renders
     Decimal as string — never float).
   - `GET /api/orders` → list of `{client_order_id, venue_order_id, instrument, side,
     type, qty, limit_price, status, filled_qty, avg_fill_price}` from
     `engine.router.tracked_orders()` (money as strings).
   - `GET /api/kpi` → `{realised_pnl, fees_paid, equity_end, sharpe, sortino,
     max_drawdown, calmar}` from `engine.perf` (money as strings; ratios as numbers).
   - `GET /api/events` → **SSE**: register a queue via `engine.bus.add_queue()`,
     stream events as `text/event-stream` (serialize each event to JSON with money as
     strings), and `remove_queue` in a `finally` on disconnect (mirror dccd).
3. **No mutation endpoints** — there is deliberately no POST to place/cancel orders;
   the UI is read-only. Document this.
4. Decimal-as-string: define response models (pydantic) or a JSON encoder so every
   monetary field serializes as a string; assert it in tests.

## Tests (via `.venv`, FastAPI `TestClient` — no real server)

- Build a paper `engine`, run a short strategy so there are positions/orders/fills,
  then `TestClient(create_app(engine))`:
  - `GET /api/health` → 200, reports `mode == "paper"`.
  - `GET /api/positions` → the expected instrument(s); **money fields are JSON strings**
    (assert `isinstance(body[0]["net_qty"], str)` and the value is exact).
  - `GET /api/orders` → the tracked orders with string money.
  - `GET /api/kpi` → realised PnL (string) matches `engine.perf.realised_pnl()`; KPI
    numbers present.
- SSE: emit a `FillEvent` on `engine.bus` and assert a consumer reading `/api/events`
  receives it (use `TestClient` streaming or a short read); the queue is removed on
  disconnect.
- **No-mutation**: assert there is no route that places an order (e.g. a POST to a
  plausible order path returns 404/405).

## Verification on real data

In-process (paper engine via `TestClient` is the real data). Run a strategy through
the engine, then hit `/api/positions` + `/api/kpi` and assert the JSON (Decimal
strings) matches the engine's `tracker`/`perf` exactly. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`interfaces.api` — read-only FastAPI over the engine (positions/orders/KPI + SSE; money as Decimal strings)."
- ADR: read-only API (no order placement from the web), Decimal-as-string JSON, SSE via EventBus queues.
- Status/roadmap: deferred to leaf 02.
