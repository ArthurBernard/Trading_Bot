# Audit — domain & storage (2026-07-02)

## Scope & method (files read, commands run)

Deep audit of the pure `domain/` layer and the `storage/` persistence layer of
the live-trading engine, on branch `develop` (clean tree). Every file in scope
was read in full:

- `trading_bot/domain/`: `errors.py`, `fill.py`, `instrument.py`, `money.py`,
  `order.py`, `performance.py`, `position.py`, `signal.py`, `__init__.py`
- `trading_bot/storage/`: `sqlite_store.py`, `__init__.py`
- Tests: `trading_bot/tests/domain/{test_errors,test_fill_position,test_instrument,test_money,test_order,test_performance,test_signal}.py`,
  `trading_bot/tests/storage/test_sqlite_store.py`

Read-only checks run (no source modified, no commit, no network):

- `pytest trading_bot/tests/domain trading_bot/tests/storage -q` → **229 passed**
  (domain/ modules at 94–100% line coverage; `sqlite_store.py` 96%).
- `mypy trading_bot/domain trading_bot/storage` → **Success, no issues** (11 files).
- `grep -rn "float" trading_bot/domain/` → no float leak in money paths
  (all hits are in docstrings, the sanctioned `from_float`, and the deliberate
  `equity_array` KPI boundary).
- `grep -rn "from trading_bot.{transport,brokers,storage,application,interfaces}"
  trading_bot/domain/` → **empty**: domain purity holds (the sole application
  reference is a lazy, function-local import inside `SqliteStore.attach`, in the
  storage layer, which is correct).
- `grep -rn "TODO|FIXME|HACK|XXX" trading_bot/domain trading_bot/storage` → none.
- Targeted runtime probes (from repo root) confirming: the `parse_kraken_pair`
  Tezos mis-parse, the float-through-constructor leaks, the missing `orders`
  migration, the always-`NULL` `orders.ts`, the process-global `Decimal` context,
  the cross-mode `fill_id` collision, NaN/Inf `target_qty` acceptance, and the
  Decimal↔`str`↔SQLite round-trip (which is **exact**, including scientific
  notation and trailing-zero scale).

## Summary

The layers are in good shape: domain purity is intact, the money-as-`Decimal`
discipline is real (SQLite stores `str(Decimal)` TEXT and round-trips exactly,
verified for `1E+3`, `1E-8`, `100.00`, huge integers), the order state machine is
correctly guarded with an explicit transition table, and `Position` fold math
(average-cost, partial close, full close, flip, fees) is correct and matched by a
strong incremental-vs-batch equivalence test. The most serious gaps are: (1) the
domain value objects (`Order`, `Fill`, `Signal`) **accept raw `float`** for money
fields at runtime — the `money()` guard is never applied inside the
constructors, so a `float` price silently enters the PnL source of truth and only
explodes later (or never); (2) there is **no schema migration for the `orders`
table** while one exists for `fills`, so any future column drift corrupts
persistence with an `OperationalError`; (3) `parse_kraken_pair` **mis-parses
`XTZUSD` (Tezos) to `XT/USD`** — a wrong instrument, i.e. wrong-market risk; and
(4) `avg_fill_price` / average-entry divisions run under the **process-global
28-digit `Decimal` context**, silently rounding a repeating division — a book
"exact to the cent" claim that is only true to 28 significant digits.

| ID | Severity | Location | Title |
|----|----------|----------|-------|
| D-1 | Critical | `order.py:197-210`, `fill.py:91-98`, `signal.py:131-135` | Money fields accept raw `float` at runtime — the domain never applies its own `money()` guard |
| D-2 | High | `sqlite_store.py:86-99, 82-121` | No migration for the `orders` table — schema drift corrupts persistence (only `fills` is migrated) |
| D-3 | High | `instrument.py:264-272` | `parse_kraken_pair("XTZUSD")` → `XT/USD`: Tezos and other X/Z-prefixed altnames mis-parsed to the wrong instrument |
| D-4 | High | `order.py:346`, `position.py:211` | `avg_fill_price` / avg-entry divide under the process-global 28-digit `Decimal` context — repeating divisions silently rounded |
| D-5 | Medium | `sqlite_store.py:307, 98` | `orders.ts` is always written `NULL` — the column is dead and the order timestamp is lost |
| D-6 | Medium | `order.py:322-336` | Over-fill is rejected even when within `fill_tolerance` — a market order that slightly over-delivers raises instead of closing |
| D-7 | Medium | `signal.py:142-148` | `SignalMode.TARGET_QTY` accepts `NaN` / `Infinity` and any non-finite `Decimal` as a target quantity |
| D-8 | Medium | `sqlite_store.py:241-253` | No `busy_timeout` / WAL-concurrency hardening set explicitly; relies on the CPython `sqlite3` 5 s default |
| D-9 | Medium | `sqlite_store.py:311-351` | `fill_id` is the sole primary key across all modes — the same venue id under two modes collides and silently keeps the first tag |
| D-10 | Medium | `instrument.py:127-136` | `normalise` strips a leading `X` from *any* 4-char code starting with `X`, even non-legacy tickers |
| D-11 | Low | `performance.py:127-133` | `_check_aligned` docstring claims "non-empty fill series" but empty input is accepted (and returns `()`) |
| D-12 | Low | `signal.py:150`, `fill.py:110-127` | NaN/out-of-context money raises `decimal.InvalidOperation`, not a domain `*Error` — inconsistent taxonomy |
| D-13 | Low | `errors.py:113-134, 137-157` | `InstrumentMismatch` / `InsufficientFunds` are exported and documented but never raised/constructed by the audited layers |
| D-14 | Low | `sqlite_store.py:305-307` | `_row_to_order` never restores `reject_reason` / `fill_tolerance`; a persisted rejected order loses its reason on reload |
| D-15 | Info | `sqlite_store.py:184-186, 241-253` | `":memory:"` store is silently useless (fresh connection per op) — documented, but a foot-gun |
| D-16 | Info | `fill.py:68-71` | `Fill.ts` is `int` ms; `Order`/storage carry no execution time — no aware-UTC `datetime` anywhere (by design) |
| D-17 | Info | `position.py:196-197` | Fees are subtracted from `realised_pnl` even while an opening fill realises no gross PnL — correct, but makes `realised_pnl` non-zero on a pure open |

## Findings

### D-1 [Critical] Money fields accept raw `float` at runtime — the domain never applies its own `money()` guard

**Location**: `trading_bot/domain/order.py:197-210` (`Order` fields + `__post_init__:212-227`),
`trading_bot/domain/fill.py:91-98` (`Fill` fields + `__post_init__:100-127`),
`trading_bot/domain/signal.py:131-135`.

**Evidence**: The dataclasses type money fields as `Money` (= `Decimal`) but the
`__post_init__` validators only check *ranges*, never the *type*:

```python
# order.py
qty: Money
...
if self.qty <= 0:            # a float 1.5 passes: 1.5 <= 0 is False
    raise OrderError(...)
```

Runtime probe:

```
Order('c', inst, OrderSide.BUY, 1.5, OrderType.MARKET)   # float qty accepted
  -> o.qty = 1.5  (type float);  o.qty <= 0 -> False
Fill('T1','c', inst, OrderSide.BUY, 1.5, 30000.1, 0.0, 1)  # all floats accepted
  -> f.qty=1.5 f.price=30000.1 f.fee=0.0
```

`money()` in `money.py:74-85` *does* reject `float`/`bool`, but nothing in the
domain routes construction through it. Because `Money = Decimal` is a bare alias
(not a `NewType` or wrapper), mypy also treats a `float` literal leniently at many
call sites, so the strict-typing net does not catch it either.

**Impact**: This is the single most important invariant of a real-money engine
("all money is `Decimal`, never float"). A `float` price silently enters a
`Fill` — the declared **PnL source of truth** — and either (a) is persisted as
`str(30000.1)` with the binary-rounding tail already baked in, or (b) blows up
only later, non-locally, when the fill is folded (`Position.with_fill`:
`Decimal + float` → `TypeError`, confirmed at runtime). A fill that is recorded
but never folded persists a corrupt price forever. The guard the whole `money`
module exists to provide is bypassed at every domain boundary.

**Recommendation**: Coerce-and-validate every money field in each `__post_init__`
via `money()` (which rejects `float`/`bool`), e.g. `object.__setattr__(self,
"qty", money(self.qty))` for frozen dataclasses, or an explicit
`if not isinstance(self.qty, Decimal): raise OrderError(...)`. Do it for
`Order.{qty,limit_price,stop_price,filled_qty,avg_fill_price,fill_tolerance}`,
`Fill.{qty,price,fee}`, `Signal.{target,strength}`. Alternatively promote `Money`
to a `typing.NewType` so mypy rejects a bare `float` at call sites too.

### D-2 [High] No migration for the `orders` table — schema drift corrupts persistence

**Location**: `trading_bot/storage/sqlite_store.py:82-121` (`_SCHEMA`, all
`CREATE TABLE IF NOT EXISTS`), `217-219` (init applies schema then
`_migrate_fills_tags` — for fills only).

**Evidence**: `_migrate_fills_tags` (`:587-606`) exists precisely because
`CREATE TABLE IF NOT EXISTS` never alters an existing table; it inspects
`PRAGMA table_info(fills)` and `ALTER TABLE ... ADD COLUMN` for `mode`/`venue`.
There is **no equivalent for `orders` or `state`**. Runtime probe against a v0
`orders` table missing the (newer) `ts` column:

```
old-orders-shape upsert FAILED: OperationalError
  table orders has no column named ts
```

**Impact**: This is the declared **reconciliation source** on restart. The moment
the `orders` schema evolves (as `fills` already has), every existing production
database becomes un-writable: `upsert_order` throws `OperationalError`, the engine
cannot persist order state, and reconciliation-after-restart — the invariant that
protects against duplicate/lost live orders — is broken. The precedent for the
fix already exists (the fills migration), which makes the asymmetry a latent
bomb rather than an accepted limitation.

**Recommendation**: Add a general, versioned migration step (a `state` row
`schema_version`, or a per-table `PRAGMA table_info` + `ADD COLUMN` sweep
mirroring `_migrate_fills_tags`) covering `orders` (and `state`). At minimum,
generalise `_migrate_fills_tags` into a `_migrate(conn)` that also reconciles the
`orders` columns. See also D-5 (`ts` column).

### D-3 [High] `parse_kraken_pair("XTZUSD")` → `XT/USD`: X/Z-prefixed altnames mis-parsed

**Location**: `trading_bot/domain/instrument.py:263-272` (altname branch of
`parse_kraken_pair`), interacting with `normalise:127-136`.

**Evidence**: Runtime probe:

```
parse_kraken_pair('XTZUSD')  -> XT/USD      # WRONG, should be XTZ/USD (Tezos)
```

The altname branch tries `qlen in (4, 3)`: for `XTZUSD` (len 6) it first tries a
4-char quote `TZUSD`? no — it slices `code[-qlen:]`, so `qlen=4` → quote `ZUSD`,
`normalise("ZUSD")="USD"` ∈ `_FIAT` → it returns `Symbol(code[:-4], "ZUSD")` =
`Symbol("XT", "ZUSD")` = `XT/USD`. The base `XT` is nonsense; the true base is
`XTZ`. The bug is that the loop stops at the *first* `qlen` whose normalised quote
looks like a known quote, without checking the base is a plausible asset — and the
`Z` inside `XTZ` is misread as a legacy fiat prefix.

**Impact**: Silent wrong-instrument decoding. If a Kraken pair string for Tezos
(or any altname whose base ends in a letter that collides with a fiat prefix,
e.g. anything ending `...Z` before `USD`) reaches this parser, the engine builds
a position/order on the wrong symbol. In a live router that is a wrong-market
order — real money into the wrong book. It is masked today only because the tests
cover just BTC/ETH/LTC/XRP altnames, none of which trip the `Z`-inside-base case.

**Recommendation**: In the altname branch, validate that the *remaining base* is a
plausible non-empty asset (e.g. length ≥ 2 after normalisation, and not itself a
bare quote), and prefer the *longest base* / shortest known quote rather than the
first 4-char slice. Consider matching against a known quote suffix set the way
`parse_binance_symbol` does (longest-quote-first over a curated list) instead of
blindly slicing 4 then 3. Add a regression test for `XTZUSD`, `XTZEUR`.

### D-4 [High] Average-price divisions run under the process-global 28-digit `Decimal` context

**Location**: `trading_bot/domain/order.py:346` (`avg_fill_price = notional /
new_filled`), `trading_bot/domain/position.py:211`
(`avg_entry = (avg_entry*old_mag + price*add_mag) / total_mag`).

**Evidence**: These are the only `/` operations on money in the domain. `Decimal`
division uses the *thread/process-global* `decimal.getcontext()` (default
`prec=28`), which rounds. Runtime probe:

```
getcontext().prec = 28
BUY 1 @ 100, BUY 2 @ 100.0000001  -> avg_fill_price = 100.0000000666666666666666667
```

The trailing `...667` is a rounded 28-significant-digit result, not the exact
repeating decimal. The domain docstrings promise "exact in `Decimal`" and
"reconcile to the cent", and the module never pins or localises the context.

**Impact**: (1) Correctness: `avg_fill_price` and `avg_entry_price` are *not*
exact for any non-terminating quotient; two engines with different ambient
`decimal` contexts (a caller may have set `prec` elsewhere — it is global mutable
state) will compute *different* averages from the same fills, breaking the
reconciliation/determinism guarantee. (2) The 28-digit default is generous but
arbitrary; nothing documents or guards it. This undercuts the headline "exact to
the cent" claim precisely where division is unavoidable.

**Recommendation**: Either (a) use a `decimal.localcontext()` with an explicitly
pinned precision inside the two division sites so the result is deterministic
regardless of ambient context, or (b) document that averages are prices (not
book totals) and are quantised to the instrument's `price_precision` before use —
and keep the exact `notional`/`net_qty` around for the reconciliation math. At
minimum, pin the context so the value is reproducible across processes.

### D-5 [Medium] `orders.ts` is always written `NULL` — dead column, lost timestamp

**Location**: `trading_bot/storage/sqlite_store.py:307` (the last UPSERT value is
a literal `None`), schema `:98` (`ts INTEGER`).

**Evidence**:

```python
(   ...
    str(order.filled_qty),
    None if order.avg_fill_price is None else str(order.avg_fill_price),
    None,          # <- the ts column, unconditionally None
),
```

Runtime probe: `SELECT ts FROM orders` → `(None,)` after a real upsert.

**Impact**: The `orders` table carries no time information at all — there is no
`Order.ts`/`created_at` field in the domain either, so an order's submission time
is unrecoverable from the reconciliation source. For a live engine, "when did we
place this order" is basic forensic/reconciliation data. The column is currently
pure dead weight (and it is the same column that D-2's missing migration will trip
over). It also means `fills` can be filtered by time but `orders` cannot.

**Recommendation**: Either add a real `created_at`/`updated_at` to the `Order`
aggregate and persist it, or drop the `ts` column from the schema. Do not leave a
half-wired column, especially given D-2.

### D-6 [Medium] Over-fill is rejected even within `fill_tolerance`

**Location**: `trading_bot/domain/order.py:331-336` (`if new_filled > self.qty:
raise OrderError("over-fill")`), which runs *before* any tolerance logic
(`_is_filled_within_tolerance:353-363` only handles the *under*-fill case).

**Evidence**: Runtime probe:

```
Order qty 1 (MARKET); venue reports fill 1.0005
  -> OrderError: over-fill: 1.0005 exceeds order qty 1
```

The tolerance is one-sided: an under-fill within 0.1% closes to FILLED, but a
*slight over-delivery* (equally common on market orders and on venues that round
up lot sizes) hard-errors.

**Impact**: A live market order that the venue fills for marginally more than
requested — a routine occurrence — raises an exception in the order path instead
of recording the actual executed quantity. Depending on the caller, that either
crashes the fill handler or leaves the order stuck OPEN while the venue considers
it done, desyncing local state from the broker (a reconciliation hazard). The
domain's own tolerance philosophy ("venues leave sub-tick dust") should apply
symmetrically.

**Recommendation**: Allow a small over-fill within `fill_tolerance` (clamp
`filled_qty` to `qty`, or accept and mark FILLED with a logged residual), and
only hard-reject a *material* over-fill. Add tests for `filled = qty * (1 + tol/2)`.

### D-7 [Medium] `SignalMode.TARGET_QTY` accepts `NaN` / `Infinity`

**Location**: `trading_bot/domain/signal.py:142-148` (`__post_init__`: the
`EXPOSURE` branch bounds-checks, but `TARGET_QTY` has *no* validation beyond the
comment "accepts any signed Decimal (incl. 0 = flat); no bound").

**Evidence**: Runtime probe:

```
Signal.target_qty(inst, Decimal('NaN'), ts=1).target   -> NaN
Signal.target_qty(inst, Decimal('Infinity'), ts=1)     -> Infinity
```

**Impact**: A non-finite target quantity flows into `delta_to`
(`target_net_qty - position.net_qty`), producing a `NaN`/`Inf` order size that
either poisons downstream arithmetic silently or is sent as a garbage order size.
For an execution engine, an unbounded/NaN target is a direct route to a malformed
live order. (`EXPOSURE` is protected because `NaN` fails the `-1 <= NaN <= 1`
bound — but via `decimal.InvalidOperation`, see D-12.)

**Recommendation**: Reject non-finite targets in `target_qty` (and `strength`):
`if not self.target.is_finite(): raise SignalError(...)`. Same for the exposure
target to make the failure a clean `SignalError`.

### D-8 [Medium] No explicit `busy_timeout` / concurrency hardening on the connection

**Location**: `trading_bot/storage/sqlite_store.py:241-253` (`_conn` opens a bare
`sqlite3.connect(str(self._path))` with only `row_factory` set); WAL is set once
in `_SCHEMA` but per-connection pragmas are not.

**Evidence**: `grep busy_timeout|isolation_level trading_bot/storage/` → empty.
The store's docstring advertises "trivially safe for being shared across threads"
and WAL mode. Probe: a bare connection reports `busy_timeout=5000` — but that is
the **CPython `sqlite3` default**, not something the store guarantees; it is not
set in code, so a change of stdlib default, a different runtime, or a caller that
resets it removes the only thing preventing immediate `SQLITE_BUSY` under
concurrent writers.

**Impact**: With a fresh connection per op and multiple engine
threads/processes writing (order + fill streams via the bus), a writer contending
for the WAL write lock will raise `sqlite3.OperationalError: database is locked`
the moment the implicit default is not there. Silent reliance on a default is
fragile for a persistence layer that explicitly courts concurrency.

**Recommendation**: In `_conn`, set `conn.execute("PRAGMA busy_timeout=5000")`
(and reaffirm `synchronous=NORMAL`) explicitly per connection. Consider
`sqlite3.connect(..., timeout=5)`. Document the concurrency model actually
supported.

### D-9 [Medium] `fill_id` primary key collides across modes; first tag silently wins

**Location**: `trading_bot/storage/sqlite_store.py:101-112` (`fills` PK is
`fill_id` alone), `:334` (`INSERT OR IGNORE`).

**Evidence**: Runtime probe recording the same `fill_id` first as `paper` then as
`live`:

```
cross-mode same fill_id -> rows: 1, kept mode = paper
```

The `mode`/`venue` tags exist to keep paper/testnet/live as *separate* PnL series,
but the identity key does not include `mode`/`venue`.

**Impact**: Paper and live venues can (and do — testnet reuses id spaces, paper
ids are engine-generated) produce colliding `fill_id`s. `INSERT OR IGNORE`
silently drops the second one, so a *live* fill can be discarded because a *paper*
fill happened to share the id — a live PnL series missing a real execution, or a
live fill mis-tagged `paper`. This directly threatens the "fills are the source of
truth" and "keep live/testnet separate" guarantees.

**Recommendation**: Make the primary key `(fill_id, mode, venue)` (or add a
surfaced composite unique constraint), so a genuinely different-mode execution is
a distinct row and the append-only/no-double-count guarantee is per series.

### D-10 [Medium] `normalise` strips a leading `X` from any 4-char code starting with `X`

**Location**: `trading_bot/domain/instrument.py:127-136`.

**Evidence**: The code strips the `X` prefix on any 4-char code whose first char
is `X`, without checking the remainder is a known crypto:

```python
elif code[0] == "X":
    code = stripped     # unconditional for 4-char X-codes
```

Probe: `normalise('XETC') -> 'ETC'`, `normalise('XREP') -> 'REP'` (intended for
legacy Kraken codes), but the same unconditional rule is what enables the D-3
mis-parse and would silently mangle any real 4-char ticker starting with `X`
(e.g. a hypothetical `XCAD`-style token) into a 3-char code. The comment at
`:129-131` claims "none in our universe", which is an assumption, not a guard.

**Impact**: Correctness depends on an unverified closed-world assumption about the
asset universe. Combined with D-3 it produces silently wrong instruments. As new
assets list, a 4-char `X…` ticker will be corrupted with no error.

**Recommendation**: Only strip the `X` prefix when the stripped code is a *known*
crypto (mirror the `Z`-branch's `stripped in _FIAT` check with a crypto allow-list
or a "stripped code is itself canonical" check), or restrict stripping to the
doubled-prefix legacy forms (`XX…`, and the `_KRAKEN_TO_CANONICAL` aliases).

### D-11 [Low] `_check_aligned` docstring claims "non-empty" but empty input is accepted

**Location**: `trading_bot/domain/performance.py:127-133`.

**Evidence**: Docstring: *"Require a non-empty fill series and a price series of
equal length."* The body only checks `len(fills) != len(prices)`; it never rejects
empty. Probe: `perf.pnl([], []) == ()` (and `test_empty_inputs_are_empty` relies
on this). So the "non-empty" clause is false.

**Impact**: Documentation/contract drift only — the empty-input behaviour is
deliberate and tested. But a reader trusting the docstring may assume a guard that
is not there.

**Recommendation**: Fix the docstring to "a price series of equal length (empty is
allowed)"; the function name `_check_aligned` is accurate, the prose is not.

### D-12 [Low] NaN/out-of-context money raises `decimal.InvalidOperation`, not a domain error

**Location**: `trading_bot/domain/signal.py:142-147` (exposure bound check on a
`NaN` `Decimal`), and generally any `Fill`/`Order` range comparison against a
malformed `Decimal`.

**Evidence**: Probe: `Signal.exposure(inst, Decimal('NaN'), ts=1)` raises
`decimal.InvalidOperation` (from the `-1 <= NaN <= 1` comparison), not
`SignalError`. The domain's error taxonomy (`errors.py`) is otherwise carefully
built so callers can catch `TradingBotError`.

**Impact**: A caller catching the documented `SignalError`/`TradingBotError` will
miss `InvalidOperation` and see an uncaught stdlib exception. Minor, but it leaks
the `decimal` implementation through the domain's error contract.

**Recommendation**: Validate `is_finite()` up front (see D-7) and raise the
appropriate domain error, so no `decimal.InvalidOperation` escapes.

### D-13 [Low] `InstrumentMismatch` / `InsufficientFunds` exported but unused by the audited layers

**Location**: `trading_bot/domain/errors.py:113-134` (`InstrumentMismatch`),
`:137-157` (`InsufficientFunds`); both re-exported in `domain/__init__.py`.

**Evidence**: `InstrumentMismatch` *is* used (raised in `position.py:187`), so it
is fine. `InsufficientFunds`, `RiskLimitBreached`, `NoCapability`,
`LiveTradingNotEnabled`, `ConfigError`, `MissingOrder`, `BrokerError` are not
raised anywhere in `domain/` or `storage/` — they are the taxonomy for other
layers (brokers/application). Within scope they are dead, but that is by design
(a shared error vocabulary). Flagging for completeness: verify each is actually
raised somewhere in the codebase; a truly never-raised class is dead code.

**Impact**: None within scope; a documentation/completeness note. If any of these
is raised nowhere in the whole repo, it is dead code to remove.

**Recommendation**: Confirm (outside this scope) that `InsufficientFunds`,
`RiskLimitBreached`, `NoCapability`, etc. are raised by the broker/application
layers; drop any that are not.

### D-14 [Low] `_row_to_order` does not restore `reject_reason` (or `fill_tolerance`)

**Location**: `trading_bot/storage/sqlite_store.py:529-554` (`_row_to_order`),
schema `:86-99` (no `reject_reason` / `fill_tolerance` columns).

**Evidence**: `_row_to_order` sets `status`, `filled_qty`, `avg_fill_price`,
`venue_order_id` but never `reject_reason`; the schema has no column for it, nor
for `fill_tolerance`. A rebuilt REJECTED order therefore has `reject_reason ==
None`, and every rebuilt order silently reverts to `DEFAULT_FILL_TOLERANCE`.

**Impact**: On reload from the reconciliation source, the *reason* a live order
was rejected is lost — forensically relevant for a real trading system. A
non-default `fill_tolerance` is also silently discarded, so a reloaded order
could re-close at a different tolerance than the live one did. Both are
low-frequency but real state-fidelity gaps for a store that claims "the persisted
row is the truth".

**Recommendation**: Add `reject_reason` (and, if `fill_tolerance` is ever set
non-default, `fill_tolerance`) columns and round-trip them; couple this with the
D-2 migration work.

### D-15 [Info] `":memory:"` store is silently useless with per-op connections

**Location**: `trading_bot/storage/sqlite_store.py:184-186` (docstring warns),
`:241-253` (fresh connection per op).

**Evidence**: The docstring honestly warns that an in-memory DB "does not persist
across operations" because each op opens a new connection (each gets a *distinct*
empty in-memory DB). So `SqliteStore(":memory:")` yields a store where every read
returns empty.

**Impact**: A test or caller that reaches for `":memory:"` (the natural choice)
gets a silently no-op store — every write vanishes. Documented, so Info, but it is
a foot-gun that will surprise.

**Recommendation**: Either reject `":memory:"` with a clear error, or support it by
holding a single shared connection when the path is in-memory (`shared cache` /
`:memory:` with a kept connection).

### D-16 [Info] No timezone-aware `datetime` anywhere — timestamps are `int` ms (by design)

**Location**: `trading_bot/domain/fill.py:68-71`, `signal.py:110-113`; storage
`ts INTEGER`.

**Evidence**: All timestamps are documented as "milliseconds since the Unix epoch
(UTC)" and stored as `INTEGER`. This sidesteps every naive-vs-aware `datetime`
bug — there are none because there are no `datetime`s. The audit brief asks about
naive/aware UTC handling; the answer is that the layers deliberately avoid
`datetime` entirely, which is a sound choice.

**Impact**: None — noting it as a positive design decision and confirming there is
no naive/aware hazard in scope. The only caveat is the D-5 `orders.ts`-always-NULL
gap (an order has no timestamp at all).

**Recommendation**: None; keep the epoch-ms convention. Just fix D-5.

### D-17 [Info] `realised_pnl` is non-zero on a pure opening fill (fees)

**Location**: `trading_bot/domain/position.py:196-197`.

**Evidence**: `realised_pnl = self.realised_pnl - fill.fee` runs on *every* fill,
including a pure open (`test_fee_on_opening_fill_only` asserts `realised_pnl ==
-12` after a single opening buy with fee 12). The module docstring is explicit
that fees always reduce realised PnL "on opening fills as well as closing ones".

**Impact**: None — it is documented, tested, and defensible (fees are a realised
cash cost regardless of whether gross PnL was realised). Flagging only because a
naive consumer might expect `realised_pnl == 0` while a position is purely open;
it will be negative by the accrued fees.

**Recommendation**: None; the behaviour is intentional and correct. Optionally
surface a `gross_realised_pnl` (pre-fee) view for reporting clarity.
