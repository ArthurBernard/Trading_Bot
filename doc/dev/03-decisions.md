# 03 — Decisions (ADR journal)

Newest first. Each entry: the decision, the *why*, and (when relevant) what was
rejected. `/finish-task` appends accepted decisions; `/abandon-task` records
rejected approaches as tombstones.

---

### 2026-06-20 Order lifecycle as an explicit state machine (PR #8)

**Decision.** `domain/order.py` models the order as a **mutable, stateful
aggregate** with a typed `OrderStatus` machine (NEW→SUBMITTED→OPEN→
PARTIALLY_FILLED→FILLED, plus CANCELLED/REJECTED) — replacing the legacy
`{None,'open','canceled','closed'}`. State changes only through five guarded
transitions; `apply_fill` accumulates an exact Decimal quantity-weighted average
price and closes to FILLED when the unfilled fraction is below `fill_tolerance`
(0.1%, ported from legacy `check_vol_exec`). Over-fills are rejected;
`client_order_id` is mandatory (idempotency invariant).

**Why.** An order has stable identity and a long fill-by-fill life; a mutable
aggregate guarded by explicit transitions keeps the machine the single source of
truth without copy-threading noise. The tolerance handles venues leaving sub-tick
dust. **Rejected:** immutable copy-on-transition (noise without added safety).

---

### 2026-06-20 Decimal-guarded Money & Kraken normalisation in `domain/` (PR #7)

**Decision.** `domain/money.py` accepts only `str`/`int`/`Decimal`; a `float`
(and `bool`) raises `TypeError`. The single sanctioned float entry point is
`from_float()`, which routes through `str(value)` to get the shortest
round-tripping decimal (`from_float(0.1) == Decimal("0.1")`). `quantize` defaults
to `ROUND_DOWN` so an order never overshoots a tick/lot. `domain/instrument.py`
normalises Kraken assets with the alias table **mined from the dccd Kraken
adapter** (`XBT→BTC`, `XDG→DOGE`) plus Kraken's legacy 4-char `X`/`Z` prefix rule.

**Why.** Money correctness is a core invariant — `Decimal(0.1)` already carries
binary error, so floats must never enter silently. Reusing dccd's table keeps the
two repos consistent instead of inventing a divergent mapping.

---

### 2026-06-20 Bootstrap the dev environment & Claude workflow (Phase 0)

**Decision.** Bring the repo up to the dccd/fynance standard *before* writing any
new trading code: `pyproject.toml` (replacing `setup.py`), ruff/mypy/pytest/
interrogate, pre-commit, GitHub Actions CI (3.11–3.13), Git Flow (`develop`/
`master`), `CLAUDE.md`, `.claude/` (workflow.json + hooks + settings), and this
`doc/dev/` orientation pack.

**Why.** The tooling is a no-regret prerequisite, identical across the triptych,
and independent of any product decision. Doing it first lets every later epic flow
through the `/pick-task → /plan → /execute-leaf → /finish-task` loop. Product
orientation is fixed only at the "north star" level for now and deepened epic by
epic, to avoid premature over-design.

---

### 2026-06-20 Full rewrite, not incremental modernization

**Decision.** Rebuild trading_bot from scratch as a hexagonal, async-first engine.
Park the pre-2026 code under `trading_bot/legacy/` (reference/spec only, excluded
from tooling) and replace it module by module.

**Why.** The legacy code (2019–2020, Python 3.7/3.8, `multiprocessing` server/
clients over a socket, "not yet working") is far behind dccd/fynance and unsafe to
extend for live trading. A clean rewrite maximises harmony with dccd and lets the
safety invariants be designed in rather than retrofitted. **Rejected:** keeping the
multiprocessing architecture and modernizing in place — too much risk carried
forward for a money-handling system.

---

### 2026-06-20 Scope: execution **and** orchestration

**Decision.** trading_bot is both the execution engine *and* the orchestrator that
wires dccd (data) + fynance (signals) + brokers into one end-to-end trading app.

**Why.** It is the natural conductor of the triptych — the only pillar that needs
all three at runtime. **Rejected:** a thin execution-only library (would still need
an orchestrator somewhere) and a fully standalone bot embedding its own data/
backtest (duplicates dccd/fynance).

---

### 2026-06-20 Multi-exchange by design, Kraken first

**Decision.** Define a `Broker` port + registry so multiple exchanges are
first-class, but implement **only Kraken** (plus a `PaperBroker`) at MVP; other
venues declare capabilities and raise early until built.

**Why.** Multi-exchange shape is cheap to design up front and expensive to retrofit;
implementing one venue well (Kraken, the legacy target) keeps the MVP focused.

---

### 2026-06-20 Paper-trading by default; Decimal money; reconcile-don't-assume

**Decision.** `PaperBroker` is the default; live requires explicit opt-in +
credentials + confirmation. All monetary values are `Decimal`. The engine
reconciles local state against broker-reported orders/balances/fills on startup
and after any disconnect; fills are the sole source of truth for PnL. Every order
passes a `RiskManager` (limits + kill-switch). Orders carry a client-order-id for
idempotent submission.

**Why.** This is a money-handling system; correctness and safety dominate. These
are the hard invariants in `CLAUDE.md`. Whether live becomes a default beyond the
MVP is **deferred**.

---

### 2026-06-20 Keep the name `trading_bot` for now

**Decision.** Defer choosing a final "nice" project name; keep the `trading_bot`
package and repo for the rewrite.

**Why.** Naming is reversible and non-blocking; revisiting it later (a go-live
milestone) avoids churn now. Tracked as a deferred decision to reopen.
