---
plan: web-ui
kind: global
status: planning
roadmap: "- [ ] **E9 — Web UI.** FastAPI + Jinja2 dashboard (positions/orders/PnL), mirroring dccd's UI."
release_on_done: false
---

# E9 — Web UI

## Goal

A **read-only** web dashboard mirroring dccd's UI: a FastAPI app exposing the
engine's state (positions, orders, PnL/KPI) as JSON + an SSE live stream, and a
Jinja2 dashboard that is a **pure HTTP client** of that API. Launch it with
`trading-bot serve`. Opens `trading_bot/interfaces/{api,ui}/`.

**Invariants**: the UI is **read-only** — it never places orders (paper or live).
Money is **Decimal strings** in JSON (never float). Everything offline-testable via
FastAPI `TestClient` over a paper `Engine` (no real server, no network).

## Decomposition

1. **api** — `interfaces/api/app.py`: FastAPI `create_app(engine)` with read-only `/api/positions|orders|kpi|health` + SSE `/api/events`.
2. **ui** — `interfaces/ui/`: Jinja2 dashboard (positions/orders/PnL+KPI), pure HTTP client of the api, live via SSE; served by the same app. Plus a `trading-bot serve` CLI command.

## Leaf checklist

- [ ] 01 api — feat/web-api — high
- [ ] 02 ui — feat/web-ui — high (depends on 01)

## Dependencies

- 02 depends on 01. Serial in the main worktree.

## Done criteria

- `create_app(engine)` serves read-only positions/orders/KPI JSON (money as Decimal
  strings) + an SSE event stream; the Jinja2 dashboard renders them and live-updates;
  `trading-bot serve` launches it.
- The UI never calls the application layer directly and never places an order.
- `ruff`/`mypy`/`pytest` green via `.venv` (0 unexpected skips); the API + a UI smoke
  are tested with FastAPI `TestClient` over a paper engine.
- Last leaf (02) removes the E9 line from `07-roadmap.md` and updates `06-status.md`.
