# Vendored third-party assets

- **uPlot v1.6.31** (`uplot.min.js` + `uplot.min.css`) — MIT, from jsdelivr
  (https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/). Self-hosted (no CDN, no
  build step) so the dashboard's PnL chart works offline behind loopback; the
  IIFE bundle exposes the global `uPlot`.
