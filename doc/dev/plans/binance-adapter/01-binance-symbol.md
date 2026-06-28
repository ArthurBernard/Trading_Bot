---
plan: binance-adapter/01-binance-symbol
kind: leaf
status: planned
complexity: low
depends: []
parallel: false
branch: feat/binance-symbol
pr: ""
---

# Binance symbol parsing (venue-neutral ↔ Binance pair codes)

## Goal

Teach the pure `domain/instrument.py` to translate Binance pair codes to/from the
canonical `Symbol`. Binance concatenates **without a separator** (`BTCUSDT`,
`ETHBTC`, `BNBUSDT`) using **canonical asset codes** (no Kraken-style `X`/`Z`
prefixes, no `XBT` alias — Binance Bitcoin is `BTC`). So the existing generic
`to_venue_symbol` path already renders Binance pairs correctly; the missing piece
is the **inverse** parse, needed to rebuild `Order`s/`Fill`s from Binance
responses.

## Files to change

- `trading_bot/domain/instrument.py` — add `parse_binance_symbol(pair) -> Symbol`
  and a module-level `_BINANCE_QUOTES` table; export `parse_binance_symbol` in
  `__all__`. (Do **not** change `to_venue_symbol` behaviour — just confirm/keep
  the generic branch; optionally add an explicit `venue == "binance"` no-op branch
  for clarity.)
- `trading_bot/tests/domain/test_instrument.py` — add Binance cases.

## Steps

1. Add `_BINANCE_QUOTES`: an ordered tuple of Binance quote assets, **longest
   first** so the suffix match is unambiguous:
   `("FDUSD", "USDT", "USDC", "TUSD", "BUSD", "DAI", "BTC", "ETH", "BNB", "EUR",
   "GBP", "TRY", "AUD", "BRL", "USD")`.
2. `parse_binance_symbol(pair)`:
   - honour an explicit separator first (`/`, `-`, `_`) → `Symbol(base, quote)`
     (mirrors `parse_kraken_pair`);
   - else upper-case and find the **first** quote in `_BINANCE_QUOTES` (longest
     first) that the string **ends with** and leaves a non-empty base →
     `Symbol(base, quote)`;
   - else `raise ValueError(f"cannot parse Binance pair {pair!r}")`.
   - `Symbol.__post_init__` already canonicalises both legs (`normalise` passes
     Binance codes through unchanged), so no Binance-specific alias table is
     needed.
3. Confirm `Symbol("BTC", "USDT").to_venue_symbol("binance") == "BTCUSDT"` (the
   generic `f"{base}{quote}"` branch). Add an explicit `binance` branch only if it
   improves readability — behaviour must not change for any other venue.

## Tests

- `parse_binance_symbol("BTCUSDT") == Symbol("BTC", "USDT")`;
  `"ETHBTC" == Symbol("ETH", "BTC")`; `"BNBUSDT" == Symbol("BNB", "USDT")`;
  `"ETHFDUSD" == Symbol("ETH", "FDUSD")` (longest-quote-first wins over `USD`).
- Round-trip for every LS1-style pair: for each base in
  `{BTC, ETH, BNB, SOL, XRP, ADA, DOGE, …}`,
  `parse_binance_symbol(Symbol(base, "USDT").to_venue_symbol("binance")) ==
  Symbol(base, "USDT")`.
- Separator forms: `parse_binance_symbol("BTC/USDT")`, `"BTC-USDT"`, `"btc_usdt"`
  all equal `Symbol("BTC", "USDT")`.
- `to_venue_symbol("binance")`: `BTC/USDT → "BTCUSDT"`, `ETH/BTC → "ETHBTC"`
  (asserts **no** `XBT` aliasing leaks into Binance, unlike Kraken).
- `pytest.raises(ValueError)` on an unparseable code (e.g. `"BTC"` alone, `"ZZZZ"`).

## Verification on real data

Not a data path on its own — but make the parse table **honest** against the live
venue: in the `binance-rest` leaf's public smoke, assert that the symbols Binance
returns from `GET /api/v3/exchangeInfo` for the configured pairs each round-trip
through `parse_binance_symbol` ↔ `to_venue_symbol("binance")`. (Recorded here so
the parse table is checked against reality, not just hand-picked cases.)

## Closeout

- CHANGELOG (Added): "`domain.instrument.parse_binance_symbol` — parse Binance
  concatenated pair codes (`BTCUSDT`) into canonical `Symbol`s."
- ADR: only if a non-trivial choice arises (e.g. quote-table ordering / ambiguity
  policy). Otherwise none.
- Status/roadmap: no change (deferred to leaf 02).
