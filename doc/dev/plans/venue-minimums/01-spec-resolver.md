---
plan: venue-minimums/01-spec-resolver
kind: leaf
status: planned
complexity: medium
depends: []
parallel: false
branch: feat/instrument-spec-resolver
pr: ""
---

# 01 — the instrument-spec resolver

## Goal

Give the application layer one way to obtain a metadata-rich `Instrument` for
any `(exchange, symbol)`, reusing the venue adapters' existing public
builders. New `trading_bot/application/instrument_specs.py`:

- `class InstrumentSpecResolver`: `async resolve(exchange: str, symbol:
  Symbol) -> Instrument`. Dispatch by exchange name (the strings the units
  already carry — read how `service_factory._build_broker` matches venues and
  mirror the same names/aliases): `kraken` → keyless `KrakenBroker`,
  `binance` → keyless `BinanceBroker`, each constructed lazily ONCE and used
  only for the public `instrument()` call. Any other exchange → bare
  `Instrument(symbol)`.
- **Cache**: per `(exchange, str(symbol))`, process-lifetime (a plain dict —
  venue minimums change on venue announcements, not intraday; a daemon
  restart refreshes). Concurrent first calls for the same key must not
  double-fetch (an `asyncio.Lock` per resolver is enough at this scale).
- **Permissive fallback**: on ANY fetch failure (`BrokerError`, transport
  errors, timeout) → return bare `Instrument(symbol)`, cache the bare result
  (do not re-hammer a broken endpoint every tick), and remember the failure
  so the caller can emit its one-per-lifetime warn (expose e.g.
  `resolver.degraded: set[str]` of exchange names, or return a
  `(instrument, resolved: bool)` pair — pick ONE, document it; the runner
  wiring in leaf 02 consumes it).
- Constructor takes optional pre-built adapter instances (dependency seam for
  tests); default builds them keyless.

Application-level on purpose: `application/` already imports the adapters
(`service_factory`); the `Broker` port stays untouched; `brokers/` gains no
cross-adapter imports.

## Files to change

- `trading_bot/application/instrument_specs.py` — **new** (above; full
  numpydoc, module docstring carries the layering rationale + cache policy).
- `trading_bot/tests/application/test_instrument_specs.py` — **new**:
  - `test_dispatch_kraken_binance_bare` (fake adapters injected: right one
    called per exchange, unknown exchange → bare instrument, no fetch);
  - `test_cache_single_fetch` (two resolves, one adapter call; concurrent
    `asyncio.gather` first-calls → still one);
  - `test_fetch_failure_falls_back_bare_and_caches` (adapter raises
    `BrokerError` → bare instrument, failure remembered, second call does
    not re-fetch);
  - `test_keyless_construction` (default resolver builds adapters without
    credentials — no env needed, nothing raises at construction).
- A `-m network` test (same file, `@pytest.mark.network` — mirror the
  repo's existing network-marked tests' style): real resolve of
  `BTC/USDT` on binance and `BTC/USD` on kraken → assert `min_notional`
  (binance) and `min_qty` (kraken) are not None and positive.

## Steps

1. Read `service_factory._build_broker` (exchange-name matching),
   `kraken.py:465` / `binance.py:532` (`instrument()` signatures, error
   modes), and both constructors (keyless path).
2. Implement + tests. All three gates green
   (`~/.pyenv/versions/trading_bot_env/bin/python -m pytest`, `-m ruff check
   trading_bot/`, `-m ruff format --check trading_bot/`).

## Tests

Listed above (offline with injected fakes + one opt-in network test).

## Verification on real data

Run the network test for real (`python -m pytest -m network
trading_bot/tests/application/test_instrument_specs.py -v`) and report the
actual fetched values: Binance BTC/USDT `min_notional` (expect ≈ 5 USDT),
`qty` step/precision; Kraken BTC/USD `ordermin` (expect ≈ 0.00005 BTC).
Also resolve the full 14-symbol USDT set the dashboard trades (script or
parametrized run) and report min_notional per symbol — these are the values
leaf 02's policy will act on. Public endpoints only, no credentials, no
writes anywhere.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "instrument-spec resolver — venue
  minimums/precisions fetched once per `(exchange, symbol)` from the public
  endpoints, permissive fallback (#XX)."
- ADR: application-level resolver over port extension (why `Broker` port
  stays untouched; cache + degrade policy).
- Tick leaf 01 in `00-plan.md`; archive per `/finish-task`.
