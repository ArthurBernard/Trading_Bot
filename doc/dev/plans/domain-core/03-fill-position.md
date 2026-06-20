---
plan: domain-core/03-fill-position
kind: leaf
status: done
complexity: medium
depends: [02]
parallel: false
branch: feat/domain-fill-position
pr: ""
---

# Fill + Position (net from fills)

## Goal

`Fill` — a broker-confirmed execution, the **source of truth for PnL** — and
`Position`, the net exposure per instrument rebuilt from fills (handles increases,
partial closes, flips, and fee accrual). Pure, Decimal, mypy-strict.

## Files to change

- `trading_bot/domain/fill.py` — new.
- `trading_bot/domain/position.py` — new.
- `trading_bot/domain/__init__.py` — export.
- `trading_bot/tests/domain/test_fill_position.py` — new.

## Steps

1. **fill.py**: frozen `Fill(fill_id, client_order_id, instrument, side, qty, price,
   fee, ts)` — all amounts Decimal; immutable.
2. **position.py**: `Position(instrument, net_qty, avg_entry_price, realised_pnl,
   fees_paid)` + classmethod `from_fills(fills)` that folds an ordered fill sequence:
   weighted-average entry on size increases; realise PnL on decreases/flips; accrue
   fees. One instrument per position (reject mixed-instrument fills).
3. Keep pure; deterministic.

## Tests

- Single buy → `net_qty`/`avg_entry_price` correct.
- Buy then partial sell → realised PnL on the sold part; remaining qty/avg correct.
- **Flip** (sell more than held) → net flips sign, realised PnL on the closed part,
  new avg = the flipping fill's price.
- Fees reduce realised PnL / accrue in `fees_paid`.
- Mixed-instrument fills raise.

## Verification on real data

Pure layer. Feed a **realistic multi-fill sequence including a flip** and assert
`net_qty`, `avg_entry_price`, `realised_pnl`, `fees_paid` against hand-computed
Decimal expectations. `pytest` green, `mypy` strict clean.

## Closeout

- CHANGELOG (Added): "Fill and Position (net exposure rebuilt from fills)."
- ADR: none (fills-as-source-of-truth already in `03-decisions.md`).
- Status/roadmap: deferred to leaf 05.
