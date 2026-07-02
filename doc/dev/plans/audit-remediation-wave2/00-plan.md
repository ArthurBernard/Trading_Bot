---
plan: audit-remediation-wave2
kind: global
status: executing
roadmap: "Audit remediation — wave 2 (deferred, needs care)."
release_on_done: true
---

# Audit remediation — wave 2 (needs care)

## Goal

Close the deferred "needs care" audit findings — the architectural/robustness items
held back from wave 1 (`doc/dev/audit/`): async SQLite off the event loop, supervisor
concurrency locking, and the Binance rate-limiter + broker error/PnL fidelity. Each
leaf ships as one atomic PR into `develop`. Standing constraint: **paper/testnet
only, no real orders** — all verification offline or against `PaperBroker`.

## Decomposition

Four leaves, largely file-disjoint (leaves 03/04 both touch `brokers/binance.py` +
`kraken.py` in different functions — expect a trivial add-only merge overlap).

1. **storage-async-io** — `A-2` move the synchronous SQLite open→write→commit→close
   off the event loop (thread executor / writer task) on the hot order/fill path;
   `D-8` set WAL + `busy_timeout` explicitly; `D-9` make `fill_id` not collide
   across modes (composite key incl. mode/venue + migration).
2. **supervisor-concurrency** — `A-3` per-unit `asyncio.Lock` so a scheduler `step`
   can't race a control-plane `set_mode`/`stop`/`remove`; `A-10` reconcile
   `remove_unit`'s hand-inlined teardown with `stop`.
3. **broker-limiter-robustness** — `B-6` weight-aware Binance limiter (per-endpoint
   weights, `X-MBX-USED-WEIGHT`, 418 IP-ban backoff, `Retry-After`); `B-9` surface
   `Retry-After` on a `retry=False` 429; `B-7` detect a Binance error returned inside
   a JSON array.
4. **broker-fill-fidelity** — `B-8` `open_orders` partial-fill must use the venue's
   actual average fill price, not the limit/stop price (wrong PnL basis); `B-10`
   Kraken private-WS reconcile-on-fill-gap + robust `ts` parsing.

## Leaf checklist

- [ ] 01 storage-async-io — fix/storage-async-io — high
- [ ] 02 supervisor-concurrency — fix/supervisor-concurrency — high
- [ ] 03 broker-limiter-robustness — fix/broker-limiter-robustness — high
- [ ] 04 broker-fill-fidelity — fix/broker-fill-fidelity — medium

## Dependencies

All four are independent (`parallel: true`). CHANGELOG/ADR entries are added at PR
closeout (orchestrator) to avoid `[Unreleased]` merge conflicts across branches.

## Done criteria

Four PRs open into `develop`, each with `pytest`/`ruff`/`mypy` green and its findings
verified. On merge → `/release` (v0.9.0). The long tail of Low/Info items stays on
the roadmap.
