---
plan: canary-roundtrip/02-canary-cli
kind: leaf
status: planned
complexity: medium
depends: [01]
parallel: false
branch: feat/canary-cli
pr: ""
---

# 02 — `trading-bot canary`

## Goal

A Typer command that makes the canary a one-liner:

```
trading-bot canary [--exchange binance] [--symbol BTC/USDT] [--mode paper]
                   [--budget 100] [--max-cost 2] [--probe-offset-pct 50]
```

- **paper (default)**: builds a self-contained canary engine via
  `build_engine` — paper broker funded with `--budget` quote units and a
  mark price for the symbol resolved from the REAL venue spec path where
  cheap (the epic-C resolver gives minimums; the mark can be a fixed
  sane default or a fetched last close — READ what leaf 01 chose for its
  harness and reuse; document the choice), strict paper ON, scratch/no
  store by default (`--db` optional to persist the evidence trail);
- sizes qty = the venue-legal minimum for the symbol (resolver;
  `min_order_ratio` semantics NOT involved — the canary places exact legal
  minimums), refusing to start if the implied cost bound exceeds
  `--max-cost`;
- runs `run_canary`, prints the evidence table (one line per check:
  PASS/FAIL, name, expected, observed — mirror the existing CLI table
  style in `interfaces/cli/_render.py`), exits 0/1;
- `--mode testnet|live`: NOT implemented in this leaf — print a clear
  "arrives with leaf 03" error (the flag exists so scripts are stable).
- READ `interfaces/cli/main.py` for command registration conventions
  (naming, help text, error handling) and mirror them exactly.

## Files to change

- `trading_bot/interfaces/cli/main.py` — the command.
- `trading_bot/interfaces/cli/_render.py` — a small evidence-table renderer
  IF the existing helpers don't already fit (read first).
- `trading_bot/application/canary.py` — only if a small seam is missing
  (e.g. a `size_for_minimums(instrument, budget)` helper better lives
  there); keep additive.
- `trading_bot/tests/interfaces/test_cli.py` (or the existing CLI test
  module — locate it) — tests below.

## Steps

1. Read the CLI module + leaf 01's harness; implement.
2. All three gates green.

## Tests

- `CliRunner` invocation of `canary` (paper): exit 0, output contains the
  evidence table with all-PASS; a seeded-failure engine (monkeypatched
  oracle input) → exit 1 and the FAIL line printed.
- `--mode testnet` → the not-yet-implemented error, exit != 0.
- Sizing refusal: `--max-cost` below the implied minimum cost → refuses
  before placing anything.

## Verification on real data

Run `trading-bot canary` FOR REAL from the repo (paper default, real
resolver against the public endpoint for the minimums): paste the full
printed evidence table and the exit code. Then `--symbol DOGE/USDT` (the
1-USDT-floor outlier) to prove the sizing follows the real per-symbol
minimum. No `var/`, no port 8000.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "`trading-bot canary` — one-command
  platform self-test (paper), evidence table + exit code (#XX)."
- ADR: none expected (CLI wiring of a decided design); state so.
- Tick leaf 02 in `00-plan.md`; archive per `/finish-task`.
