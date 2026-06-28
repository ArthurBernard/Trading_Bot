---
plan: strategy-runner/01-strategy-spec
kind: leaf
status: done
complexity: medium
depends: []
parallel: false
branch: feat/strategy-spec
pr: "#30"
---

# Strategy spec — declare & load a strategy

## Goal

Define how a strategy is declared and loaded: a `Strategy` (config + a **signal
callable** that takes a bars frame and returns a domain `Signal`). Replaces the
legacy importlib `get_signal({-1,0,1})` loading. Pure-ish, typed.

## Files to change

- `trading_bot/application/strategy.py` — new; `Strategy`, `StrategySpec`, loader.
- `trading_bot/application/__init__.py` — export them.
- `trading_bot/tests/application/test_strategy.py` — new.

## Steps

1. Read `domain/signal.py` (`Signal.exposure` / `Signal.target_qty`),
   `domain/instrument.py`, `application/config.py` (`StrategyConfig` skeleton:
   `name`, `symbol`), and the legacy spec
   (`trading_bot/legacy/strategy_manager.py` + `strategies/example/strategy.py`'s
   `get_signal`).
2. Define `SignalFn = Callable[[BarsT], Signal]` where `BarsT` is the bars frame
   type (a polars `DataFrame` of OHLC, or a typed wrapper — pick the simplest that
   the data-feed leaf will produce; document it). The callable returns a domain
   `Signal` for the strategy's instrument.
3. `Strategy` dataclass: `name`, `instrument` (from `StrategyConfig.symbol`),
   `signal_fn: SignalFn`, optional `reference_qty` (max position size for
   fractional-exposure signals), optional warmup/lookback (min bars before the
   signal is valid). Method `evaluate(bars) -> Signal` that calls `signal_fn` and
   validates the returned `Signal` is for this instrument.
4. A **loader**: build a `Strategy` from a `StrategyConfig` + a resolvable
   `signal_fn` reference (an importable `"module:function"` string **or** a passed
   callable — keep it simple and safe; document the resolution). This is the modern
   replacement for the legacy importlib path — no exec of arbitrary files.
5. Ship a tiny built-in example signal (e.g. a moving-average crossover using
   fynance) for tests/docs.

## Tests (via `.venv`)

- A `Strategy` with a hand-written `signal_fn` (returns `Signal.exposure(...)`) →
  `evaluate(bars)` returns the expected `Signal`; an instrument mismatch raises.
- The fynance-backed example signal (MA crossover) on a known bar series → expected
  long/flat/short exposure at the crossover points.
- Loader resolves a `"module:function"` reference to a callable and builds the
  `Strategy`; an unresolvable reference raises a clear error.
- Warmup: fewer than `lookback` bars → a defined behaviour (flat `Signal` or raise — document).

## Verification on real data

In-process. Run the example MA-crossover `signal_fn` over a realistic OHLC series
(synthetic-but-realistic, or a fixture) and assert the signals flip at the expected
crossovers — and that no signal at bar *t* used any bar `> t`. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`application.Strategy` — declare/load a strategy (config + signal callable → domain `Signal`)."
- ADR: the strategy contract (signal callable → `Signal`) + the safe loader (no arbitrary-file exec, vs legacy importlib).
- Status/roadmap: deferred to leaf 03.
