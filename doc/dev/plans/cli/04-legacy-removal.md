---
plan: cli/04-legacy-removal
kind: leaf
status: done
complexity: low
depends: [02, 03]
parallel: false
branch: chore/remove-legacy
pr: "#43"
---

# Legacy removal — delete the superseded pre-2026 tree

## Goal

Delete the `trading_bot/legacy/` modules now fully superseded by the rewrite (the
whole order→fill→position→strategy→CLI path exists natively), and tidy
packaging/docs/README. The last E7 leaf — closes the E7 roadmap line and marks the
MVP. **Reference, not deletion-by-default**: remove only what is truly superseded and
referenced nowhere outside `legacy/`.

## Files to change

- `trading_bot/legacy/` — remove the superseded modules: `strategy_manager.py`,
  `orders.py`, `orders_manager.py`, `performance.py`, `cli.py`, `bot_manager.py`,
  `_server.py`, `_client.py`, `_connection.py`, `_containers.py`, `data_requests.py`,
  `exchanges/`, `order/`, `tools/` (and `_exceptions.py`, `logging.ini`) — i.e. retire
  the legacy tree once nothing live imports it.
- `pyproject.toml` — drop the now-unneeded `--ignore=trading_bot/legacy` /
  `legacy` excludes from pytest/ruff/mypy/coverage/interrogate config if the tree is gone.
- `README.md`, `doc/dev/01-overview.md`, `doc/dev/02-architecture.md` — remove the
  "parked under legacy/" framing; the rewrite is the implementation now.
- `.gitignore`, `MANIFEST.in` — drop legacy-specific lines.

## Steps

1. **Prove nothing live references legacy**: grep the package (excluding
   `legacy/` itself and tests that intentionally target it) for
   `trading_bot.legacy` / `from trading_bot.legacy` — must be **zero** hits outside
   legacy. If anything still imports it, STOP and surface it (that capability isn't
   actually reimplemented yet).
2. `git rm -r` the superseded legacy modules. If a sub-bit is still genuinely useful
   as reference (unlikely), keep only that file with a one-line note — but default to
   removing the lot.
3. Remove the legacy exclusions from `pyproject.toml` tooling config and re-run all
   gates to confirm nothing depended on the excludes.
4. Update README + `doc/dev/01-overview.md`/`02-architecture.md` to drop the
   "legacy parked" language (keep a one-line historical note pointing at git history).

## Tests (via `.venv`)

- The full suite stays green with the legacy tree gone and the excludes removed
  (`.venv/bin/python -m pytest -q`).
- `ruff`/`mypy` now scan the whole package (no legacy exclude) and pass.
- A grep asserts there is no `trading_bot.legacy` import anywhere in the live package.

## Verification on real data

The engine's behaviour is unchanged (legacy was never imported by the live path) —
re-run the end-to-end `trading-bot run` over an OHLC fixture and confirm identical
results to before removal. Gates via `.venv`.

## Closeout

- CHANGELOG (Removed): "Deleted the superseded pre-2026 `trading_bot/legacy/` tree (rewrite complete through the MVP CLI)."
- ADR: note the MVP "first light" reached; the legacy reference tree retired (history in git).
- Status/roadmap: **remove the E7 line** from `07-roadmap.md`; mark E7 done + the MVP reached in `06-status.md`.
