---
plan: portfolio-strategy/02-portfolio-feed
kind: leaf
status: planned
complexity: medium
depends: [01]
parallel: false
branch: feat/portfolio-feed
pr: ""
---

# Portfolio data feed — N-coin, common-index daily bars (freshness-gated)

## Goal

A **multi-instrument** causal feed that assembles, for a universe of N coins,
their daily bars from **dccd's Binance store** on a **common date index**, and
gates each rebalance on **"all N coins have today's close"** (the LS1 dossier §7:
a missing/stale bar silently changes the cross-section). Causal: only closed daily
bars, growing windows — never a future bar.

## Files to change

- `trading_bot/application/portfolio_feed.py` — new (or extend
  `application/data_provider.py` if that is where `feed_for`/`DccdFeed` live —
  read first and match the existing module boundary).
- `trading_bot/tests/application/test_portfolio_feed.py` — new.

## Steps

1. Read `application/data_provider.py` (`feed_for`, `DccdClient`, `DccdFeed`) and
   `application/data_feed.py` (the causal `DataFeed` protocol: `__iter__` yields
   growing `frame[:t+1]` windows, `latest()`), to reuse the single-coin dccd read
   and the causality contract.
2. Implement `PortfolioFeed`:
   - construct from a `universe: Sequence[Symbol]`, a per-coin dccd
     `DataSourceConfig`-equivalent (Binance, `span = 86400` for daily), an injected
     `DccdClient | None` (offline-testable), and `data_path`;
   - read each coin's daily bars via the same dccd path `feed_for` uses;
   - **align** on a common date index (inner-join on bar timestamp): a rebalance
     date is only emitted when **every** coin has that day's closed bar;
   - iterate **causally**: at step *t* yield a `Mapping[Symbol, pl.DataFrame]` of
     each coin's window up to and including day *t* (so the signal sees ≥ lookback
     closes, e.g. LS1 needs ≥ 200);
   - `latest()` → the full aligned per-coin frames.
3. Freshness gate: expose the asof timestamp of the latest common date; if a coin
   is missing the latest day, that day is simply not emitted (the cross-section is
   never computed on a partial universe). Log (never raise) a coin lagging the
   others.

## Tests

- Offline, with a **fake `DccdClient`** returning canned daily bars per coin:
  - common-index alignment: coins with mismatched date ranges yield only the
    intersection of dates; a coin missing the latest day → that day not emitted.
  - causality: window at step *t* contains no timestamp > day *t* for any coin;
    windows grow monotonically.
  - the per-coin window at the final step has ≥ the configured lookback rows when
    the fixture provides them.
- The asof timestamp equals the latest common date's close ms.

## Verification on real data

**Mandatory (data path).** Against the **real dccd Binance store** (the 10 LS1
coins, `*-USDT`, daily): build a `PortfolioFeed`, drain it, and assert (a) every
emitted rebalance date has a closed bar for **all 10** coins, (b) the final window
has ≥ 200 daily closes per coin (LS1's SMA-200 need), (c) no window contains a
future bar. If the dccd Binance daily store is present locally, **run it and paste
the date range + per-coin row counts**; otherwise mark `@pytest.mark.network`/skip
and document how to sync it (`DEPLOY_LS1.md` §3 — the 15 alt USDT pairs added to
the dccd daemon on 2026-06-28).

## Closeout

- CHANGELOG (Added): "`application.PortfolioFeed` — N-coin common-index daily-bars
  feed from the dccd Binance store, gated on all coins having today's close."
- ADR: only if a non-trivial choice arises (e.g. inner-join vs forward-fill on a
  missing coin-day — default **inner-join / skip the day**, never forward-fill a
  stale close into the cross-section).
- Status/roadmap: deferred to leaf 05.
