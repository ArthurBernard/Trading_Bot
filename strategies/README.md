# strategies/ — **real strategies are local-only**

This directory holds trading **strategies** (the signal wrapper + its config +
its e2e tests). The rule:

> **Real strategies are NEVER committed to this repo.** Strategy logic/IP lives
> outside the (shareable, generic) engine. `trading_bot` only *executes* a
> strategy — via the generic `application.as_portfolio_signal` adapter + a config
> that names the signal by reference. The strategy itself stays on your machine.

## What is tracked vs local

- **Tracked (templates):** `example/`, `another_example/`, and this `README.md`.
  They show the shape of a strategy; they are *not* real strategies.
- **Local-only (gitignored):** everything else under `strategies/` — your real
  strategies. The `.gitignore` rule is `strategies/*` with the templates negated,
  so a new `strategies/<name>/` is ignored automatically.

## Layout of a (local) portfolio strategy

```
strategies/<name>/
  signal.py          # thin wrapper: binds a research oracle (e.g.
                     #   fynance_research...:target_weights) through
                     #   trading_bot.application.portfolio.as_portfolio_signal
  <venue>.yaml       # a PortfolioStrategyConfig (paper by default): universe,
                     #   capital, gross_cap, daily dccd data source; signal.ref ->
                     #   "strategies.<name>.signal:<fn>"
  test_e2e.py        # the strategy's live/e2e tests (run by path, network-marked)
```

Run a strategy's tests by path (they are outside the engine's `testpaths`):

```bash
python -m pytest strategies/<name>/test_e2e.py -m network -v
```

See `doc/dev/09-go-live.md` ("How a strategy like LS1 is wired") for the wiring,
the dccd resample seam, and the venue testnet/safety notes.
