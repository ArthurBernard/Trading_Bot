---
plan: ui-ux-overhaul/02-formatters
kind: leaf
status: planned
complexity: medium
depends: [01]
parallel: false
branch: feat/ui-formatters
pr: ""
---

# Leaf 02 — Shared display formatters: rounding, units, currency

## Goal

Every figure on every page becomes readable: display-rounded, thousands-
grouped, labelled with its unit (quote currency for money, base asset for
quantities, `%` for drawdown), with the **exact Decimal string preserved in a
`title` tooltip**. Rounding is display-only — formatted output is never parsed
back, and the API payloads are untouched.

## Files to change

- `trading_bot/interfaces/ui/static/format.js` (new)
- `trading_bot/interfaces/ui/templates/base.html` (include + wire helpers, CSS)
- `trading_bot/interfaces/ui/templates/overview.html`
- `trading_bot/interfaces/ui/templates/strategies.html`
- `trading_bot/interfaces/ui/templates/orders.html`
- `trading_bot/interfaces/ui/templates/pnl.html`
- `trading_bot/interfaces/ui/templates/logs.html`
- `trading_bot/interfaces/ui/templates/dashboard.html` + `static/app.js`
  (legacy single-engine view: include format.js, swap its formatters)
- `trading_bot/tests/interfaces/test_dashboard.py` (smoke: pages reference
  format.js; static mount serves it)

## Steps

1. **`static/format.js`** — a small namespace (e.g. `tbFmt`), vanilla JS, no
   deps, exposing at least:
   - `money(v, {dp = 2, currency = null})` → grouped `en-US` display string,
     fixed `dp`; `'—'` for null. Input is the API's exact Decimal string;
     `Number()` it for display only. If it doesn't parse to a finite number,
     show the raw string (never lie).
   - `qty(v, {base = null, maxDp = 8})` → grouped, trailing zeros trimmed.
   - `price(v, {quote = null})` → adaptive precision (|v| ≥ 1000 → 2 dp;
     ≥ 1 → 2–4 dp; < 1 → up to 6 significant digits).
   - `pct(v, dp = 1)` → ratio → `"12.3%"` (for max-drawdown).
   - `ratio(v, dp = 2)` → plain fixed-dp number, `'—'` for null.
   - `cell(kind, v, opts)` (or per-kind `moneyCell`/`qtyCell`/...) → a safe
     HTML fragment: formatted value, muted unit suffix
     (`<span class="unit">USDT</span>`), and `title="<exact raw> <unit>"`.
     All interpolated strings HTML-escaped (reuse/duplicate `escapeHtml`).
2. **base.html**: `<script src="/static/format.js">` before the inline
   helpers; keep `fmtMoney`/`fmtNum` as thin wrappers over `tbFmt` so nothing
   breaks mid-migration; add `.unit { color:var(--muted); font-size:.85em;
   margin-left:.18em; }` to the shell CSS.
3. **Apply per page** (units resolved client-side from the instrument string
   `BASE/QUOTE`, and from leaf 01's `quote` on strategies/KPI rows):
   - **Overview** — KPI: PnL/fees via `money` + row `quote` (null → "mixed"
     tooltip), Sharpe/Sortino/Calmar via `ratio`, **Max DD via `pct`** (header
     `Max DD (%)`); positions: `qty`+base, `price`+quote, PnL/fees+quote;
     open orders likewise.
   - **Strategies** — Realised PnL via `money` + status `quote`.
   - **Orders** — qty/filled via `qty`+base, prices via `price`+quote, fee via
     `money`+quote.
   - **PnL** — stats table (equity / realised / unrealised) via `money` +
     strategy quote (from `/api/strategies`); chart tooltip/axis values via
     `money` (no suffix inside the canvas; put the currency in the section
     heading, e.g. `Equity over time — USDT`, when known).
   - **Logs** — fill/order lines: `qty`+base, `price`+quote.
   - **dashboard.html/app.js (legacy)** — same treatment via format.js.
4. Column headers state the unit when uniform for the whole column; otherwise
   the unit rides per-cell as the muted suffix.

## Tests

- Template smoke tests: each unified page's HTML references
  `/static/format.js`; `GET /static/format.js` is 200 with the expected
  symbols (string containment is fine).
- No behavioural server change — full suite + ruff must stay green.

## Verification on real data

`python -m pytest` (paper-engine-backed dashboard tests) + manual spot-check:
with the TestClient, fetch `/api/positions` and confirm the raw payload still
carries exact Decimal strings (unchanged serialization). Final visual pass on
the live dashboard is the maintainer's.

## Closeout

- CHANGELOG `[Unreleased]` → `### Changed`: display formatting line.
- ADR note: the "display-only rounding, exact value on hover" convention.
- Tick 00-plan checklist; frontmatter `status: done`, fill `pr:`.
