# Vendored third-party assets

- **uPlot v1.6.31** (`uplot.min.js` + `uplot.min.css`) — MIT, from jsdelivr
  (https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/). Self-hosted (no CDN, no
  build step) so the dashboard's PnL chart works offline behind loopback; the
  IIFE bundle exposes the global `uPlot`.

- **Fonts** (`fonts/martian-mono-*.woff2`, `fonts/spline-sans-*.woff2`) —
  Martian Mono (OFL) + Spline Sans (OFL), latin subset. Self-hosted (no font
  CDN — the dashboard leaks nothing and works offline). Shared with dccd's UI so
  the interface matches.

- **`logo.svg` + `favicon.svg`** — the shared dccd/trading_bot brand mark
  (candlestick network), reused across the trading stack's dashboards.
