---
plan: strategy-capital/08-ui-capital-block
kind: leaf
status: planned
complexity: medium
depends: [05, 07]
parallel: false
branch: feat/ui-capital-block
pr: ""
---

# 08 — UI: the capital block on the detail page

## Goal

The money home, where the three figures finally reconcile on screen:
**Starting capital + PnL (realised / unrealised) = Total value**, with the
policy toggle, the Deposit/Withdraw modal and the ledger audit trail. Plus a
condensed Total-value column on the roster so the same numbers read at every
altitude. Pure consumer of leaf 07's routes — no new backend.

## Files to change

- `trading_bot/interfaces/ui/templates/strategy_detail.html` — the CAPITAL card
  (top of the page, above the chart), per the validated wireframe:
  - Three visually-connected cells with explicit `+` and `=` glyphs:
    Starting capital (`contributed`) + PnL (two rows: `realized`,
    `unrealized`) = Total value (`total_value`, green/red vs contributed, with
    `Δ% since funding = total/contributed − 1`). `unrealised == null`
    (stopped/flat) → "—" row + tooltip "no open position to mark", total falls
    back to contributed + realised.
  - `Withdrawable: <w> <quote>` line; policy segmented control
    (`Reinvest` = compound / `Cashout` = fixed) POSTing
    `/api/strategies/{name}/policy` with a `toast()`; live-mode gets a
    `window.confirm` first (compound grows live order sizes).
  - `[ Deposit ] [ Withdraw ]` open one shared "Adjust capital" modal (clone
    the go-live modal structure): amount input (string, validated client-side),
    radio deposit/withdraw, preview lines "New starting capital / New total
    value" (display-only arithmetic on the exact strings), `op_id` = a UUID
    minted **once per modal open** (so a double-click/retry POSTs the same
    op-id — idempotency on the wire), Confirm → POST → refresh block + toast;
    422 → inline error with the server's withdrawable figure.
  - `Ledger: N events ▸` expander listing `capital_events` from the breakdown
    (ts · type · amount · note) — the audit trail.
  - Data from `GET /api/strategies/{name}/capital`, refreshed on the page's
    existing SSE + 10s cycle; all money rendered via `tbFmt.moneyCell`
    (exact-on-hover discipline).
  - Read-only mode (`TB_READ_ONLY`): controls hidden, figures still shown.
- `trading_bot/interfaces/ui/templates/strategies.html` — roster gains a
  `Total value` column (from the extended `/api/strategies` payload of leaf 06),
  same formatter, replacing nothing (Realised PnL stays).
- `trading_bot/tests/interfaces/test_dashboard.py` — extend.

## Steps

1. Render the block read-only first (breakdown GET), then wire the modal +
   policy toggle, then the roster column.
2. `python -m pytest trading_bot/tests/interfaces/ -v` + `ruff check`.

## Tests

- Detail shell contains the capital card markup + ledger expander; roster shell
  contains the Total value header.
- Read-only shell hides Deposit/Withdraw/policy controls.
- (JS logic is exercised in the real-data pass — templates are
  server-rendered shells; keep assertions on the rendered HTML.)

## Verification on real data

On the running daemon (paper, real dccd data, both alloc1 portfolios at
allocation 100): the block shows 100 + PnL = total and the arithmetic checks
against `/api/strategies/{name}/capital` exactly; deposit 50 via the modal →
block updates to 150 without reload; double-click Confirm → still one ledger
event (op-id held); withdraw above withdrawable → the server's 422 figure
renders inline; flip policy → `configs/dashboard.yaml` updated; ledger expander
lists genesis + the deposit. Screenshot the block for the PR.

## Closeout

- CHANGELOG (Added): capital block — start/PnL/total reconciled on screen,
  deposit/withdraw modal, policy toggle, ledger trail; roster Total-value column.
- Tick leaf 08 in `00-plan.md` — **last leaf**: swap the roadmap line for the
  deferred follow-ups (live funds gate with real-key enablement; Overview money
  band; close-and-refund teardown; state polish; API quick wins), update
  `06-status.md`, archive the tree.
