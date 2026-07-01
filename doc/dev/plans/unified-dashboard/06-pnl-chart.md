---
plan: unified-dashboard/06-pnl-chart
kind: leaf
status: planned
complexity: medium
depends: [05]
parallel: false
branch: feat/dashboard-pnl-chart
pr: ""
---

# Unified dashboard — PnL chart (uPlot, live vs testnet series)

## Goal

The PnL page (and a per-strategy panel): an interactive **equity/PnL chart over time**,
drawing **live and testnet as separate series** (colour-coded), fed by leaf-05's
`GET /api/pnl`. Uses **uPlot**, self-hosted (decision in `00-plan.md`) — no build step.
Also wires the **aggregate ratio KPIs** (Sharpe/Sortino/Calmar/maxDD at exchange/total)
on the combined curve leaf 05 exposes, filling the `null`s left by leaf 02.

## Files to change

- `trading_bot/interfaces/ui/static/uplot.min.js`, `.../uplot.min.css` — **new**,
  vendored uPlot (MIT; pinned version noted in a short `static/VENDOR.md`). Shipped via
  the existing `package-data` glob.
- `trading_bot/interfaces/ui/templates/pnl.html` — the PnL page: a strategy selector, a
  mode legend/toggle (live / testnet / all), and a uPlot chart of equity over time with
  one series per mode; a small stats caption (final PnL per mode, drawdown). Inline
  `{% block scripts %}` fetches `/api/pnl`, maps to uPlot's `{data, series}` shape,
  handles resize; polling refresh (dccd-style). Reuse `base.html` helpers.
- `trading_bot/interfaces/ui/templates/base.html` — link the vendored `uplot.min.css`
  (only on the PnL page, or globally — keep it light).
- `trading_bot/interfaces/api/app.py` — ensure `/api/kpi?level=exchange|total` now
  returns the ratio KPIs computed on the combined curve (`supervisor.pnl_series`
  combined), completing leaf 02's `null`s.
- `trading_bot/tests/interfaces/test_dashboard.py` — extend (PnL page renders; the
  chart container + vendored asset are served; aggregate ratios present).

## Steps

1. Read `/api/pnl` (leaf 05) and uPlot's data/series API (from its docs / the vendored
   dist). Read `overview.html`'s KPI strip (leaf 02) to slot the now-non-null aggregate
   ratios.
2. Vendor `uplot.min.js` + `.css` into `static/`; note the version in `static/VENDOR.md`.
3. Build `pnl.html`: strategy selector → fetch `/api/pnl` → build uPlot `data`
   (`[timestamps, liveEquity, testnetEquity]`) + `series` (labelled, coloured); legend
   toggles series; a stats caption. Handle empty history (no chart, a "no fills yet"
   note) and resize.
4. Compute aggregate ratio KPIs on the combined curve; surface at
   `/api/kpi?level=exchange|total`; the Overview KPI strip now shows them.
5. `pytest` + `ruff` + `mypy` green under `trading_bot_env`.

## Tests

- PnL page renders with the chart container; the vendored `uplot.min.js` / `.css` are
  served (200). `/api/kpi?level=total` now returns non-null ratios once a curve exists.
- A JS-free assertion is enough server-side (rendering + assets + the endpoints);
  chart drawing is verified in the real-data step.

## Verification on real data

With mode-tagged fills present (from leaf-05's verification: paper + testnet), open
`http://127.0.0.1:8000/pnl`, select **alloc1**: the chart shows **two distinct series**
(live/testnet — here testnet + paper) tracking equity over time, matching the final
values `/api/pnl` reports; toggling a series hides it; the Overview KPI strip shows
non-null Sharpe/… at total. Paper/testnet only; **no real order**.

## Closeout

- CHANGELOG (Added): "Dashboard PnL chart (uPlot, self-hosted) — per-strategy
  equity/PnL over time, **live vs testnet as separate series**; aggregate ratio KPIs on
  the combined curve."
- ADR only if a non-trivial choice arises (e.g. vendoring policy for uPlot).
- Do NOT remove the roadmap line (deferred to leaf 07).
