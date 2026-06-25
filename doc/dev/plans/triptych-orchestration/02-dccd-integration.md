---
plan: triptych-orchestration/02-dccd-integration
kind: leaf
status: planned
complexity: high
depends: [01]
parallel: false
branch: feat/dccd-integration
pr: ""
---

# dccd integration — feed_for (library import)

## Goal

Build a `DataFeed` for a strategy from its declared dccd data source, via **library
import** (the resolved integration-depth decision): `dccd.Client.read` produces the
bars and (optionally) `dccd.Client.backfill` ensures stored data exists first — all
in-process, no separate service. Injectable client so offline tests use a fake.

## Files to change

- `trading_bot/application/data_provider.py` — new; `feed_for(...)` + a small dccd-client Protocol.
- `trading_bot/application/__init__.py` — export `feed_for`.
- `trading_bot/tests/application/test_data_provider.py` — new.

## Steps

1. Read `application/data_feed.py` (`DccdFeed(client, exchange, symbol, span, *, start_ns=None, ...)`,
   `InMemoryFeed`, the `time,o,h,l,c,v` schema + the dccd column mapping), the new
   `StrategyConfig.data` (leaf 01), and `dccd.Client` (`read`, `backfill`, `inventory`).
2. `feed_for(strategy: StrategyConfig, *, client=None, backfill=False) -> DataFeed`:
   - resolve `exchange`/`symbol`/`span`/`start` from `strategy.data`/`strategy.symbol`;
   - if `client is None`, construct a real `dccd.Client` (with the configured data path
     if given) — but keep the dccd type behind a small `Protocol` so tests inject a fake;
   - optionally (`backfill=True` or when no stored data) call `client.backfill(...)` to
     **drive collection** before reading — this is the orchestrator role (document it);
   - return a `DccdFeed(client, ...)` (historical replay of the causal windows). The
     existing `DccdFeed` already normalises dccd's columns and guarantees causality.
3. Keep it thin: `feed_for` is config→feed glue; the causal/replay logic stays in
   `DccdFeed`. The dccd coupling is one import + the injectable client.

## Tests (via `.venv`, offline — fake dccd client)

- `feed_for(strategy_config, client=<fake returning a canned polars frame>)` →
  a `DccdFeed` that replays the expected causal prefixes (no real dccd).
- `backfill=True` → the fake client's `backfill` is called before `read` (assert order).
- The data-source fields (exchange/span/start) are passed through to the read call
  (assert the fake recorded them).
- A strategy whose data source is missing/invalid → a clear error.

## Verification on real data

`-m network` (opt-in): if a real `dccd.Client().inventory()` reports stored OHLC for
some exchange/pair, `feed_for` over it reads via `Client.read` and replays a few
causal prefixes (monotonic ts, no lookahead). If inventory is empty, **skip** with a
clear reason — the fake-client test covers the logic. **Run** the offline suite via `.venv`.

## Closeout

- CHANGELOG (Added): "`application.feed_for` — build a DataFeed from a strategy's dccd data source (library import; optional backfill drives collection)."
- ADR: **the resolved dccd integration decision** — library import (read for feeds, backfill/stream to drive collection), no separate service; why.
- Status/roadmap: deferred to leaf 03.
