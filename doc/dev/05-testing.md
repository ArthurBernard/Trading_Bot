# 05 — Testing

Tests live in `trading_bot/tests/`. The legacy tree is excluded from collection
(`--ignore=trading_bot/legacy`). Coverage is measured on every run
(`--cov=trading_bot`). CI matrix: Python 3.11–3.13.

```bash
pytest                                 # full suite (legacy & network excluded)
pytest trading_bot/tests/test_smoke.py -v
pytest -m network                      # opt-in: real broker/sandbox E2E
```

## Layers

1. **Domain unit tests** — pure, fast: order state machine transitions, Decimal
   money math, position from fills, PnL/KPI. No I/O.
2. **Engine tests against `PaperBroker`** — the order router, risk gate,
   reconciliation, and strategy runner driven through the in-process simulator.
   This is where the *chain* is exercised without touching a real venue.
3. **Network E2E (`@pytest.mark.network`, opt-in)** — against an exchange sandbox:
   place/cancel a tiny order, read it back, reconcile. Never run by default.

## Discipline — test the chain on real data, not just the pieces

A green unit suite is **not** enough for an execution engine. For any order path:

- Run the real operation (through `PaperBroker`, or a sandbox under `-m network`).
- Read what the **broker reports back** (open orders, fills, balances).
- Compare it to what was *requested* — quantities, prices, fees, resulting position.
- Verify **reconciliation**: kill the connection mid-flight, restart, and confirm
  local state converges to the broker's truth with no duplicated or lost orders.
- Verify the **risk gate / kill-switch** actually blocks and halts.

PnL is checked against broker-confirmed **fills**, never against local optimism.
Money comparisons use `Decimal`, never float equality.

## Invariants under test (as layers land)

- Idempotent submit: replaying a submit with the same client-order-id creates no
  duplicate.
- Reconciliation convergence after disconnect.
- Rate limiter never exceeds the venue budget.
- Secrets never appear in logs (assert redaction).
