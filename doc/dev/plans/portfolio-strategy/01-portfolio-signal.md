---
plan: portfolio-strategy/01-portfolio-signal
kind: leaf
status: done
complexity: medium
depends: []
parallel: false
branch: feat/portfolio-signal
pr: "#63"
---

# Portfolio signal contract + by-reference loader + sizing helper

## Goal

Introduce the **multi-asset** analogue of the single-instrument `SignalFn`: a
`PortfolioSignalFn` that returns a **weight vector** `{Symbol: weight}` (signed
fraction of capital, Σ|w| ≤ a cap), a safe **by-reference loader** (mirroring
`_resolve_signal_fn`/`load_strategy`), and the pure **weight→quantity** sizing
helper used by the runner (leaf 03). No I/O.

## Files to change

- `trading_bot/application/portfolio.py` — new. Define:
  - `PortfolioSignalFn` = `Callable[[int, Mapping[Symbol, "pl.DataFrame"]], Mapping[Symbol, Money]]`
    (`asof_ms`, per-coin causal frames → weights). Document: weight is a **signed
    fraction of capital**; Σ|w| may be capped by the signal; the engine does not
    re-normalise (it trusts the signal's cap, see ADR in leaf 03 for the optional
    engine-side gross guard).
  - `PortfolioStrategy` (frozen dataclass): `name: str`, `universe: tuple[Symbol, ...]`,
    `signal_fn: PortfolioSignalFn`, `capital: Money`, `gross_cap: Money | None = None`.
  - `weights_to_signals(weights, *, frames|prices, capital, asof_ms) -> list[Signal]`:
    pure helper — for each `Symbol`, `qty = weight * capital / price`,
    `Signal.target_qty(Instrument(symbol), qty, ts=asof_ms)`. Skip a symbol with a
    non-positive price (raise a clear `ConfigError`/`SignalError`). Keep everything
    `Decimal` (`money(str(...))`); never float.
  - `load_portfolio_signal(ref: str) -> PortfolioSignalFn`: a **safe** loader for a
    `"module:function"` ref (reuse the import path of
    `strategy.load_strategy` — `importlib.import_module` + `getattr`, **never**
    exec a loose file). A `ref` with no `":"` is a clear `ConfigError` (no builtin
    portfolio registry yet).
- `trading_bot/application/__init__.py` — export the new public names if the
  package surface lists application use-cases (match what's already exported).
- `trading_bot/tests/application/test_portfolio_signal.py` — new.

## Steps

1. Read `application/strategy.py` (`SignalFn`, `load_strategy` — the safe import)
   and `application/run_app.py` (`_resolve_signal_fn`) to mirror the loader style;
   read `domain/signal.py` (`Signal.target_qty`, `delta_to`).
2. Define the types/dataclass above. `PortfolioStrategy` is frozen; `universe` is a
   tuple of canonical `Symbol`s.
3. Implement `weights_to_signals` purely. Decide and document the price source
   contract: it takes an explicit `prices: Mapping[Symbol, Money]` (the runner
   passes the latest close per coin) — keeps this helper pure and unit-testable
   without a frame. (The runner extracts prices from the feed in leaf 03.)
4. Implement `load_portfolio_signal` with the same safety guarantees as
   `load_strategy` (importable module only; clear `ConfigError` on a bad ref or a
   non-callable target).

## Tests

- `weights_to_signals`: `{BTC/USDT:+0.5, ETH/USDT:-0.25}`, capital `100000`,
  prices `{BTC:50000, ETH:2500}` → `Signal.target_qty` of `+1.0` BTC and `-10.0`
  ETH (exact `Decimal`); a weight of `0` yields a flat (`0`) target; a missing or
  non-positive price raises.
- A weight whose `|w|>1` (leverage) is allowed (target_qty carries its own scale)
  — assert no `[-1,1]` rejection.
- `load_portfolio_signal("trading_bot.tests.fixtures.fake_book:weights")` resolves
  and returns a callable; a no-`":"` ref and a missing attribute each raise
  `ConfigError`.
- A tiny in-repo **fake** `PortfolioSignalFn` fixture (returns fixed weights for a
  small universe) for downstream leaves to reuse — add it under
  `trading_bot/tests/fixtures/`.

## Verification on real data

Not a data path yet (pure). The real-data check lands in leaf 05 (LS1). Here, make
the contract honest: the fake fixture's weights round-trip through
`weights_to_signals` to the exact `Decimal` target quantities a hand-calc gives.

## Closeout

- CHANGELOG (Added): "`application.portfolio` — multi-asset `PortfolioSignalFn`
  (weight-vector) contract + safe by-reference loader + weight→`Signal.target_qty`
  sizing."
- ADR: the `(asof, frames) -> {Symbol: weight}` contract choice and weight =
  fraction-of-capital sizing via explicit-qty `Signal` (vs fractional exposure,
  which can't express |w|>1). Mirrors the single-instrument signal-by-reference ADR.
- Status/roadmap: no change (deferred to leaf 05).
