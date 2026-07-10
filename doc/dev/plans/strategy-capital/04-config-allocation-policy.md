---
plan: strategy-capital/04-config-allocation-policy
kind: leaf
status: planned
complexity: low
depends: []
parallel: false
branch: feat/config-allocation-policy
pr: ""
---

# 04 — Config: `allocation` + `capital_policy` fields

## Goal

Give every managed unit — including single-instrument strategies, which today
have **no money field at all** (only `reference_qty`, base units) — a declared
capital allocation and a reinvest policy, fully backward-compatible: both fields
optional, unset = exactly today's behaviour.

## Files to change

- `trading_bot/application/config.py`:
  - `StrategyConfig` (≈ line 209): add `allocation: Decimal | None = None`
    (strictly positive when set — clone the `reference_qty` validator at
    `config.py:265-271`) and
    `capital_policy: Literal["fixed", "compound"] = "fixed"`.
  - `PortfolioStrategyConfig` (≈ line 282): add the same two fields. `capital`
    **stays required** and keeps its meaning as the legacy sizing base; add a
    `model_validator` — when `allocation` is set it must equal `capital` is NOT
    required (allocation, when set, **supersedes** `capital` as the genesis
    amount; document this precedence in both docstrings). When `allocation` is
    unset, the genesis amount for a portfolio is `capital` (so every existing
    manifest keeps working unchanged).
  - Docstrings: state the ledger semantics — `allocation` seeds the genesis
    `FUNDING` event once (leaf 06), then the config value is **inert**; the
    ledger is the source of truth thereafter.
- `trading_bot/tests/application/test_config.py` (+
  `test_portfolio_config.py`) — extend.

## Steps

1. Add fields + validators; keep the existing field order/idiom (grouped
   validators, numpydoc parameter docs).
2. Confirm YAML round-trip: `to_yaml` (`config.py:666`) already dumps via
   `model_dump(mode="json")` so the new Decimal renders as an exact string;
   `from_yaml` re-parses it. No serializer work expected — test it.
3. `python -m pytest trading_bot/tests/application/test_config.py
   test_portfolio_config.py -v` + `ruff check`.

## Tests

- Defaults: absent fields validate; `capital_policy == "fixed"`;
  `allocation is None`; a legacy `{name, symbol}`-only strategy still parses.
- Validators: `allocation <= 0` rejected; `capital_policy: "banana"` rejected.
- Round-trip: `to_yaml` → `from_yaml` reproduces exact Decimals for
  `allocation` on both model kinds.
- `add_strategy`/`add_portfolio`/`remove_entry` still validate with the new
  fields present.

## Verification on real data

Load the real local manifest (`configs/dashboard.yaml`) unmodified —
it must validate identically (defaults kick in). Then a scratch copy with
`allocation: "100"` + `capital_policy: "compound"` on one portfolio round-trips
through `to_yaml`/`from_yaml` preserving exact strings.

## Closeout

- CHANGELOG (Added): per-strategy `allocation` + `capital_policy` config fields
  (inert until the capital service lands).
- Tick leaf 04 in `00-plan.md`.
