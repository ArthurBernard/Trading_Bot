---
plan: venue-minimums/02-order-prep-policy
kind: leaf
status: planned
complexity: high
depends: [01]
parallel: false
branch: feat/order-prep-policy
pr: ""
---

# 02 — round-up-or-skip order preparation in the runner

## Goal

Apply the pinned policy (see `00-plan.md`, "Pinned policy" — implement
verbatim, including the spot-sell cap and the threshold semantics) to every
rebalance leg before it reaches the router, using leaf 01's resolver to
enrich the bare instruments.

Two parts:

1. **Pure policy helper** — new `trading_bot/application/order_prep.py`:
   `prepare_leg(delta, instrument, *, position_qty, min_order_ratio) ->
   LegDecision` where `LegDecision` is a small frozen dataclass:
   `action: Literal["submit", "round_up", "skip"]`, `qty: Money` (the final
   absolute quantity to submit; meaningless for `skip`), `reason: str` (one
   human sentence for the log — exact Decimals, names the minimum that
   bound). The helper must:
   - quantize `|delta|` to the instrument's lot (`quantize_qty`) FIRST, then
     compare against the binding minimum: `min_qty` and, when `min_notional`
     and a price are known, `min_notional / price` (the caller passes the
     leg's reference price — the same latest close the sizing used);
   - apply the threshold rule (`round_up` when `threshold × min ≤ qty_q <
     min`);
   - apply the spot-sell cap: for a sell reducing a long position, final qty
     = `min(final_qty, position_qty)`; if that cap < the binding minimum →
     `skip` (reason says so). A sell that opens/extends a short (position ≤ 0
     or portfolio strategies with shorting) is NOT capped — the cap protects
     spot longs only; decide shortness from the signed `position_qty` the
     caller passes;
   - no spec (both minimums None) → `submit` with the lot-quantized qty
     (permissive, unchanged behaviour).
2. **Runner integration** — `trading_bot/application/portfolio_runner.py`:
   - hold an `InstrumentSpecResolver` (constructor-injected; `build_portfolio_runners`
     in `service_factory.py` creates one per engine/unit — read how runners
     are constructed and thread it the same way other collaborators are);
   - at leg-preparation time (where the delta becomes an `Order` — read the
     existing leg loop at portfolio_runner.py:439-465 and the order-build
     upstream of it), `await resolver.resolve(exchange, symbol)` (cached
     after the first tick), rebuild the leg's `Instrument` with the resolved
     one, call `prepare_leg`, then: `skip` → no submit + one
     `LogEvent(level="info", ...)` with the helper's `reason`; `round_up` →
     submit the bumped qty + one `LogEvent(level="info")`; `submit` →
     unchanged path;
   - degraded resolver (leaf 01's seam) → ONE `LogEvent(level="warning")`
     per unit lifetime;
   - the unit's `exchange` string is already on the unit/config — find the
     canonical source the runner can reach (mirror whatever
     `service_factory` uses to pick the broker).
   - `StrategyRunner` (single-instrument) is OUT of scope for this leaf —
     portfolio legs only (the dashboard strategies are portfolio units);
     note it in the module docstring as a follow-up seam.
3. **Config** — `trading_bot/application/config.py`: `min_order_ratio` on the
   portfolio strategy config (pydantic, Decimal via the codebase's existing
   money-field pattern, constrained `(0, 1]`, default `0.5`); threaded to the
   runner like the other per-unit fields.

## Files to change

- `trading_bot/application/order_prep.py` — **new** (pure; domain imports
  only).
- `trading_bot/application/portfolio_runner.py` — integration.
- `trading_bot/application/service_factory.py` — resolver construction +
  threading.
- `trading_bot/application/config.py` — `min_order_ratio`.
- `trading_bot/tests/application/test_order_prep.py` — **new** (policy
  table-driven tests below).
- The existing portfolio-runner test module — integration tests below.

## Steps

1. Read the leg loop + order build in `portfolio_runner.py`, runner
   construction in `service_factory.py`, config field patterns in
   `config.py`. THEN implement helper → config → integration.
2. All three gates green.

## Tests

`test_order_prep.py` (pure, table-driven — exact Decimals):
- above-min submit unchanged; between threshold×min and min → round_up to
  the exact minimum (both the min_qty-bound and the min_notional-bound
  cases); below threshold → skip with the right reason;
- lot quantization happens before the comparison (a delta that quantizes to
  zero → skip);
- spot-sell cap: sell 0.6 min with position 0.4 min → skip; sell 1.5 min
  with position 1.2 min → capped submit at 1.2 min... (verify the cap also
  re-checks the minimum); short-extending sell uncapped;
- no-spec permissive passthrough;
- threshold bounds respected (ratio 1 → never round up; tiny ratio → almost
  always round up).

Runner integration tests (fake resolver injected):
- a dust leg produces NO router submit and one info LogEvent;
- a close-to-min leg submits the bumped qty;
- degraded resolver → one warning total across two ticks;
- normal legs unchanged (regression on the existing runner tests).

## Verification on real data

Copies of both live books + REAL specs (leaf 01's resolver against the
public endpoints). Drive one real rebalance tick through the actual seams
(engine over the copied store, real dccd frame if available or the stored
last prices): report per book — every leg's decision (`submit`/`round_up`/
`skip`) with delta, binding minimum and final qty; assert **no submitted
order is below its venue minimum** and that the audit's dust case
(0.0000079 BTC ≈ 0.50 USDT vs 5 USDT min) lands on `skip`. Originals and
port 8000 untouched.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "rebalance legs respect venue minimums
  upstream — round up when ≥ `min_order_ratio` (default 0.5) of the minimum,
  skip (with a log) otherwise; spot sells capped at the held position (#XX)."
- ADR: the threshold policy + spot-sell cap semantics (why upstream rather
  than reject-and-retry; why the residual self-corrects next rebalance).
- Tick leaf 02 in `00-plan.md`; archive per `/finish-task`.
