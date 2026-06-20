---
plan: domain-core/01-primitives
kind: leaf
status: executing
complexity: medium
depends: []
parallel: false
branch: feat/domain-primitives
pr: ""
---

# Domain primitives — Money, Instrument, errors

## Goal

The zero-dependency base of the domain: exact-Decimal money/quantities,
venue-neutral `Instrument`/`Symbol` with Kraken-style normalisation, and the
domain error hierarchy. Pure, typed strict.

## Files to change

- `trading_bot/domain/__init__.py` — new; re-export the public names (keep import-pure).
- `trading_bot/domain/money.py` — new; `Decimal`-backed price/quantity helpers.
- `trading_bot/domain/instrument.py` — new; `Symbol(base, quote)` + `Instrument` + asset normalisation.
- `trading_bot/domain/errors.py` — new; error hierarchy rooted at `TradingBotError`.
- `trading_bot/tests/domain/__init__.py` — new (empty).
- `trading_bot/tests/domain/test_money.py`, `test_instrument.py`, `test_errors.py` — new.

## Steps

1. **money.py**: construction from `str`/`int`/`Decimal` only — **guard against
   `float`** (raise/round explicitly) to avoid binary error. Provide arithmetic and
   `quantize(precision)` to a tick/lot size. No float leakage in public API.
2. **instrument.py**: frozen `Symbol(base, quote)` (hashable, `__str__` = `BASE/QUOTE`);
   `Instrument` carrying optional `price_precision`/`qty_precision`. Implement
   `normalise(asset)` — `XBT→BTC`, `XDG→DOGE`, strip leading `X`/`Z` on 4-char legacy
   codes — mining the exact table from `trading_bot/legacy/exchanges/API_kraken.py`
   and the dccd Kraken adapter (`../Download_Crypto_Currencies_Data`, altname mapping).
   Add `to_venue_symbol(venue)` to render back the venue's code.
3. **errors.py**: `TradingBotError(Exception)` root; port the meaningful legacy ones
   (`OrderError`, `OrderStatusError`, `InsufficientFunds`, `MissingOrder`) from
   `legacy/_exceptions.py`, modernised (drop exchange coupling); add
   `RiskLimitBreached`, `NoCapability` (forward use).
4. Export the public API from `domain/__init__.py`.

## Tests

- `test_money`: `Decimal('0.1')+Decimal('0.2') == Decimal('0.3')`; float construction
  guarded; `quantize` to a realistic Kraken tick; no float in results.
- `test_instrument`: `'XXBTZUSD'→BTC/USD`, `'XETHZEUR'→ETH/EUR`, `XDG→DOGE`; frozen
  equality/hashing; `to_venue_symbol` round-trip.
- `test_errors`: every error subclasses `TradingBotError`; message formatting.

## Verification on real data

Pure layer (no live I/O). Build `Instrument`s from **real Kraken pair strings**
found in `trading_bot/legacy` (e.g. `XXBTZUSD`, `XETHZEUR`) and assert normalisation;
`quantize` Money against realistic Kraken tick sizes. `pytest trading_bot/tests/domain -q`
green and `mypy trading_bot/` clean (strict on domain).

## Closeout

- CHANGELOG (Added): "Domain primitives — Decimal `money`, venue-neutral
  `instrument` with Kraken normalisation, `errors` hierarchy."
- ADR: none (mechanical foundation; the Decimal-money invariant is already in
  `03-decisions.md`).
- Status/roadmap: deferred to leaf 05.
