---
plan: binance-adapter
kind: global
status: planning
roadmap: "- [ ] **E11 — Binance adapter (2nd live venue).** `BinanceBroker` REST behind the `Broker` port (HMAC-SHA256 signing vs Binance's vector; orders/balances/fills/ticker; `newClientOrderId` idempotency; testnet-capable), wired into `service_factory`. Public market data key-free; private path proven by mocks + an opt-in Binance **testnet** E2E. WS deferred."
release_on_done: false
---

# E11 — Binance adapter (2nd live venue)

## Goal

Add **Binance** as the second live venue behind the existing venue-neutral
`Broker` port — proving the multi-exchange design end-to-end (Kraken was the
first; the port, registry, `PaperBroker` and the off-by-default live gate are all
reused unchanged). `BinanceBroker` is a **REST adapter** mirroring `KrakenBroker`:
domain-types-only surface, `transport` plumbing underneath, HMAC-**SHA256**
signing verified against Binance's published vector.

**Why now (the real use case):** the next epic, the multi-asset **LS1** long/short
book, executes on ~10 **Binance USDT** pairs (daily rebalance). Its *bars* come
from the **dccd** Binance store — so this broker only does **execution**
(orders / balances / fills / ticker), not OHLC history.

**Posture (decided with the maintainer):** Binance offers a real **spot testnet**
(`testnet.binance.vision`) — unlike Kraken. So the adapter is **testnet-capable**
(configurable base URL) and ships an **opt-in `network` E2E** that does a real
place → query → cancel round-trip on the testnet with a free testnet key
(paper money, isolated). Public market data works **key-free**; the private path
is otherwise proven by **mocks + the signing vector**. Paper stays the default;
Binance (mainnet *or* testnet) sits behind the same `live_enabled` opt-in gate —
**no mainnet order is ever sent here**. WebSocket (public + user-data) is
**deferred** to a follow-up (mirrors how Kraken split WS into its own leaf).

## Decomposition

1. **binance-symbol** — `domain/instrument.py`: `parse_binance_symbol` (concatenated,
   separator-less pairs split on a known-quote table) + confirm `to_venue_symbol("binance")`.
   Pure, no I/O.
2. **binance-rest** — `BinanceBroker` REST adapter (signing vs vector, orders/balances/
   fills/ticker, `newClientOrderId` idempotency, composite venue-order-id for symbol-scoped
   cancel, testnet-capable) + mock tests + public smoke + opt-in testnet E2E; wired into
   `service_factory`/registry/exports.

## Leaf checklist

- [ ] 01 binance-symbol — feat/binance-symbol — low (→ opus)
- [ ] 02 binance-rest — feat/binance-rest — high (→ opus) (depends on 01)

## Dependencies

- 02 depends on 01 (`BinanceBroker` uses `parse_binance_symbol`). Serial.

## Done criteria

- `trading_bot/brokers/binance.py` exposes `BinanceBroker` behind the `Broker` port;
  registered/wired in `service_factory` (`binance` ∈ live venues) and exported from
  `brokers/__init__`.
- Signing matches **Binance's published HMAC-SHA256 vector**; private endpoints are
  **mock**-covered (request shaping incl. signed query + `X-MBX-APIKEY`, response parsing
  to domain types); a real **public** ticker smoke (`-m network`) passes; the **testnet**
  E2E (`-m network`) does a real place+cancel+balance round-trip when `BINANCE_API_KEY`/
  `BINANCE_API_SECRET` (testnet) are present, and **skips** cleanly otherwise.
- Binance's symbol-scoped `cancel`/`myTrades` are handled venue-neutrally (composite
  `"<SYMBOL>:<orderId>"` venue-order-id; `fills()` over a configured symbol set) — both
  documented in an ADR.
- Paper stays the default; live (incl. testnet) is behind the existing off-by-default
  `live_enabled` opt-in. Secrets only via `.env` (gitignored), never logged.
- `ruff` + `mypy` + `pytest` green via `.venv`.
- Last leaf (02) removes the E11 line from `07-roadmap.md` and updates `06-status.md`.
