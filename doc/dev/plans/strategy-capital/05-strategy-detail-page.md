---
plan: strategy-capital/05-strategy-detail-page
kind: leaf
status: planned
complexity: high
depends: [01]
parallel: false
branch: feat/strategy-detail-page
pr: ""
---

# 05 — The per-strategy detail page (`/strategies/{name}`)

## Goal

Give every strategy the deep-linkable home it lacks — the root cause of the
"hard to find your way around" feedback. The `/pnl` tab (a single-strategy view
behind a dropdown) retires **into** this page; the Strategies table slims to a
linked roster; the deploy form moves to `/strategies/new`. Nav: 5 tabs → 4
(Overview · Strategies · Orders · Logs). No capital-model work here — the page
is the *place*; the money block arrives in leaf 08.

## Files to change

- `trading_bot/interfaces/api/app.py`:
  - `_DASHBOARD_PAGES` (≈ line 1638): drop `('/pnl', …)`; add
    `('/strategies/new', 'strategies_new.html')` (order: after `/strategies`).
  - New **parameterized** page route `GET /strategies/{name}` registered
    alongside the tuple-driven shells (the tuple can't express a path param):
    renders `strategy_detail.html` with the `name` injected server-side; 404
    for an unknown unit (check `supervisor.status()` names); all data stays
    client-fetched from `/api/*` (the "shell renders no engine data" contract).
    Mind route ordering: register `/strategies/new` **before**
    `/strategies/{name}` so "new" never matches as a name.
  - `GET /pnl` → 303 redirect to `/` (bookmarks don't break).
- `trading_bot/interfaces/ui/templates/strategy_detail.html` — **new**, extends
  `base.html`: header (name · mode badge · run pill · Start/Stop/Mode/Remove
  controls — moved from the roster row, including the typed go-live modal from
  `strategies.html:82-95`), the uPlot equity chart + per-mode stats **moved
  from `pnl.html`** (same `/api/pnl?strategy=` payload, strategy pre-bound, no
  dropdown), positions (client-filtered from `/api/positions?group_by=strategy`),
  recent orders + fills (`/api/orders?history=true&strategy=` /
  `/api/fills?strategy=`). SSE `/api/events` + 10s poll fallback (Overview's
  refresh model).
- `trading_bot/interfaces/ui/templates/strategies.html` — slim to a roster:
  columns Name (**link** to detail), Kind, Mode (badge only — the `<select>`
  moves to detail), Status, Cadence, Next bar, Last eval, Realised PnL, quick
  Start/Stop. Deploy card (lines 46-77) and go-live modal move out. Summary
  chips stay.
- `trading_bot/interfaces/ui/templates/strategies_new.html` — **new**: the
  relocated deploy form (`POST /api/strategies`), link back to the roster.
- `trading_bot/interfaces/ui/templates/pnl.html` — delete.
- `trading_bot/interfaces/ui/templates/base.html` — tabs list (≈ line 253):
  drop the PnL entry. Keep uplot assets (used by the detail page).
- `trading_bot/interfaces/ui/templates/overview.html`, `orders.html` — strategy
  names become links to `/strategies/{name}` (KPI table, positions/orders rows).
- Tests: `trading_bot/tests/interfaces/test_dashboard.py` — extend (see Tests).

## Steps

1. Land the routes + `strategy_detail.html` skeleton first (header + controls),
   then migrate the pnl chart JS verbatim (it is self-contained in
   `pnl.html`'s script block; keep `tbFmt`/`tbTime` usage as is).
2. Slim the roster; relocate deploy + go-live modal; wire links everywhere a
   name renders (grep templates for the name cell renderers).
3. Redirect `/pnl`; drop the tab; delete `pnl.html`.
4. `python -m pytest trading_bot/tests/interfaces/ -v` + `ruff check`.

## Tests

- `GET /strategies/{name}` → 200 shell containing the name; unknown name → 404;
  `/strategies/new` → 200 with the deploy form; `/pnl` → 303 to `/`.
- Nav of every page shell lists 4 tabs (no `/pnl`).
- Roster rows carry `href="/strategies/<name>"`; read-only mode hides
  Start/Stop on the roster and every control on the detail page (reuse the
  existing `TB_READ_ONLY` guards).
- Existing deploy/remove/mode API tests unchanged (endpoints untouched).

## Verification on real data

Restart the daemon on the real manifest (`trading-bot start --serve`, paper,
real dccd data): from Overview click `alloc1-binance` → its detail page shows
the live equity chart (same series `/pnl` showed before), its positions/orders
only, working Start/Stop; deploy a throwaway paper strategy via
`/strategies/new`, see it appear on the roster, remove it from its detail page.
Screenshot the detail page for the PR.

## Closeout

- CHANGELOG (Added/Changed): per-strategy detail page; PnL tab folded into it;
  deploy form on `/strategies/new`; roster slimmed.
- ADR note: IA reorg — strategy detail page as the per-strategy home.
- Tick leaf 05 in `00-plan.md`.
