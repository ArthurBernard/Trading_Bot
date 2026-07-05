---
plan: ui-ux-overhaul
kind: global
status: done
roadmap: "**Dashboard UX overhaul** — readable numbers (display rounding, units, currency), visible evaluation cadence (next tick / next bar close), positions grouped by strategy, table/feed polish."
release_on_done: false
---

# Dashboard UX overhaul

## Goal

The unified dashboard (Overview / Strategies / Orders / PnL / Logs) works but is
rough for an operator:

1. **Raw numbers** — money renders as the exact Decimal string, unrounded
   (`fmtMoney` passes strings through verbatim), with no thousands grouping.
2. **No cadence** — nothing shows when the next position calculation happens
   (the daemon's APScheduler tick is not exposed; neither is a unit's bar span).
3. **No currency** — PnL / fees / prices carry no quote-currency label.
4. **No units** — max-drawdown is a bare ratio (should read as %), quantities
   don't say which asset they are.
5. **Positions can't group by strategy** — the server supports
   `?group_by=strategy` but the Overview toggle never offers it.
6. Assorted polish: tables fully re-rendered on every poll (wipes open
   `<select>`s), order statuses not colour-coded, log lines stamped with client
   receive time instead of event time, no "last updated" feedback, no strategy
   tag on the merged event feed.

Non-negotiable invariant carried through every leaf: **rounding is
display-only** — the API keeps serving exact Decimal strings, the raw exact
value stays available (e.g. in a `title` tooltip), and no formatted value is
ever parsed back into a computation.

## Decomposition

1. `01-api-read-model.md` — backend: expose `span` + `quote` on
   strategies/KPI rows, `next_tick_ts` on `/api/health` (wired from the
   daemon's APScheduler), and tag merged SSE frames with `strategy` + `ts`.
2. `02-formatters.md` — shared display formatters (`static/format.js`):
   grouped/rounded money with currency suffix + exact-value tooltip, qty/price/
   percent/ratio/date helpers; applied across every page (incl. the legacy
   single-engine `dashboard.html`).
3. `03-overview-groups.md` — Overview: "By strategy" positions grouping
   (default), persisted view preferences, summary strip (running counts, open
   orders, total PnL, next tick).
4. `04-cadence.md` — cadence made visible: next-tick countdown in the nav
   chip, per-strategy bar span + next-bar-close countdown, "updated at" stamps.
5. `05-tables-polish.md` — no-flicker refreshes (skip unchanged re-renders,
   never wipe a focused control), order-status badges, history-cap caption,
   event-feed filters + strategy tags + event timestamps, PnL stats currency +
   return %.

## Leaf checklist

- [x] 01 api-read-model — feat/ui-api-read-model — medium
- [x] 02 formatters — feat/ui-formatters — medium
- [x] 03 overview-groups — feat/ui-overview-groups — low
- [x] 04 cadence — feat/ui-cadence — medium
- [x] 05 tables-polish — feat/ui-tables-polish — medium

## Dependencies

Strictly serial (01 → 02 → 03 → 04 → 05): every leaf after 01 consumes its API
additions and/or edits the same templates. No `parallel` leaves.

> Execution note for this epic: the maintainer explicitly requested **sonnet**
> agents for these leaves (per-run override of the global "always opus" rule).

## Done criteria

- Every money figure on every page is display-rounded, thousands-grouped,
  labelled with its currency/asset, with the exact Decimal on hover.
- Max drawdown reads as a percentage; ratios are consistently formatted.
- The dashboard shows when the next evaluation tick fires (daemon mode) and
  each strategy's bar cadence + next bar close.
- Overview positions group by strategy (default view).
- Polls no longer visually rebuild unchanged tables nor wipe open controls.
- `python -m pytest` and `ruff check trading_bot/` green on every leaf; new
  API fields covered by tests in `trading_bot/tests/interfaces/`.
