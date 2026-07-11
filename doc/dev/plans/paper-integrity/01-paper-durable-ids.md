---
plan: paper-integrity/01-paper-durable-ids
kind: leaf
status: planned
complexity: medium
depends: []
parallel: true
branch: fix/paper-durable-ids
pr: ""
---

# 01 — lifetime-unique ids in PaperBroker

## Goal

`PaperBroker` ids must never collide across engine lifetimes. Today
`__init__` seeds `self._order_ids = count(1)` and `self._fill_ids = count(1)`
(`trading_bot/brokers/paper.py:250-251`), and ids are minted as
`f"PAPER-{next(self._order_ids)}"` (in `place_order`) and
`f"PAPER-FILL-{next(self._fill_ids)}"` (in `_execute`). A rebuilt engine
(fresh broker per `build_engine`) re-mints the same strings; the reused
fill id is then silently dropped by the fill-id idempotency layer in
tracker/perf/store — a real simulated fill vanishes from the book.

New scheme: a per-instance **lifetime token** in every id —
`PAPER-{token}-{n}` and `PAPER-FILL-{token}-{n}` where `token` defaults to
`uuid.uuid4().hex[:8]` and the counters still start at 1 within the lifetime.

Why not store-backed counters: `brokers/` must not depend on `storage/`
(hexagonal layering — brokers are venue adapters behind the `Broker` port);
threading a persisted counter through the port for a *simulator* buys nothing
a random token doesn't. Note this in the ADR at closeout.

## Files to change

- `trading_bot/brokers/paper.py` — new keyword-only constructor param
  `id_token: str | None = None` (None → mint `uuid4().hex[:8]`; validate
  non-empty when given). Store as `self._id_token`; use in both mint sites.
  Update the class docstring ("Deterministic id seams" comment + any
  `PAPER-{n}` mention, e.g. the `place_order` Returns section) to the new
  format and the reason (lifetime uniqueness).
- `trading_bot/tests/brokers/test_paper.py` — fix id assertions (pass
  `id_token="t"` where exact ids are asserted); add the new tests below.
- `trading_bot/tests/application/test_order_router.py` — same fix for its
  `PAPER-`-prefixed assertions (grep: 4 exact-id assertions across these two
  files).

Do NOT touch `service_factory.py`: the default (random token) is exactly what
production wants; no caller passes `id_token`.

## Steps

1. Add `id_token` param + `self._id_token` to `PaperBroker.__init__`
   (keyword-only, after the existing simulation params; reject empty string
   with `BrokerError` like the other param validations).
2. Change the two mint sites to embed the token.
3. Update docstrings (class-level, `place_order`) that show the old format.
4. Fix the four existing exact-id test assertions by passing a fixed
   `id_token` in those fixtures.
5. Add the new tests.
6. `python -m pytest && ruff check trading_bot/` green.

## Tests

In `trading_bot/tests/brokers/test_paper.py`:

- `test_ids_unique_across_instances`: two `PaperBroker` instances (no
  `id_token`), same placements → the sets of venue ids and of fill ids are
  disjoint.
- `test_id_token_deterministic`: `id_token="t"` → first placement yields
  venue id `PAPER-t-1` and fill id `PAPER-FILL-t-1`.
- `test_id_token_empty_rejected`: `id_token=""` raises `BrokerError`.
- Regression (the epic's motivating bug, unit-level): one `SqliteStore` +
  tracker; lifetime 1 broker fills an order; replay store fills into the
  tracker (as `_replay_paper_book` does); lifetime 2 broker (fresh instance)
  fills a new order wired to the same bus/store → assert the second fill IS
  recorded in the store and applied by the tracker (with the old scheme this
  fails: same `fill_id`, silently swallowed).

## Verification on real data

Run one paper cycle through the real seams (not mocks): build two engines
sequentially via `build_engine` on a scratch config + scratch SQLite db
(pattern: `trading_bot/tests/` engine fixtures), place one order in each
lifetime through `OrderRouter.submit`, then read back
`store.stored_fills()` and assert two distinct fill ids and a position equal
to the signed sum of both fills. Report the ids and the position in the leaf
PR description.

## Closeout

- CHANGELOG `[Unreleased] > Fixed`: "PaperBroker ids are unique across engine
  lifetimes (`PAPER-{token}-{n}`) — a restarted unit's fills were silently
  swallowed by fill-id idempotency, dropping real simulated fills from the
  book."
- ADR (`doc/dev/03-decisions.md`): lifetime token over store-backed counters
  (broker must not depend on storage).
- Tick leaf 01 in `00-plan.md`; archive per `/finish-task`.
