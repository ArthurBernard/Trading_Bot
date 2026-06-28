# Go-live runbook

> **Live trading is OFF by default and gated.** Paper trading is the default and
> is fully working; trading real money is a deliberate, multi-step opt-in. Nothing
> in this repository sends a real order on its own — the live venue adapter is not
> even constructed until you have explicitly opted in. **Never risk money you
> cannot afford to lose.**

This runbook is the single source of truth for *how* to go live, *what is proven*
offline, and *what still needs a real-key sandbox* before any real order. Read it
in full before flipping any switch.

---

## Status

- **Paper-trading is the default and fully working.** A fresh `AppConfig` is
  `mode: paper`, `live_enabled: false`; the engine wires a
  [`PaperBroker`](../../trading_bot/brokers/paper.py) — no venue, no key, no
  network. A fresh config can never trade real money by accident.
- **Live is off by default and gated by two independent opt-ins** plus
  credentials (see below).
- The hardening suite (`trading_bot/tests/hardening/`) proves the safety-critical
  paths **offline** against the `PaperBroker` and fault-injecting fakes. What it
  cannot prove without a real key is listed in *Proven vs pending*.

---

## The opt-in gates (all required to go live)

Going live requires **all** of the following — each is an independent gate, and
missing any one refuses with a non-zero exit / a raised
`LiveTradingNotEnabled` (or `BrokerError`) and **places no order**:

1. **`mode: live`** in the config (`AppConfig.mode`). Default `paper`.
2. **`live_enabled: true`** in the config (`AppConfig.live_enabled`). Default
   `false`. This is the explicit "I have read the runbook" opt-in — flipping
   `mode` alone is not enough. The factory
   ([`build_engine`](../../trading_bot/application/service_factory.py)) raises
   `LiveTradingNotEnabled` (pointing here) when `mode == "live"` and this is
   `false`, **before it ever looks at credentials**.
3. **Credentials** — `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` in the environment
   (via a gitignored `.env`, never committed). With `live_enabled: true` but no
   credentials the factory raises `BrokerError`.
4. **CLI acknowledgement** — `trading-bot run --live` additionally requires
   `--yes-i-understand` (or an interactive confirmation) *and* re-checks
   `live_enabled`. Missing either, the command refuses and points here.

---

## To enable live trading — the deliberate steps

1. **Read this runbook.** (You are here.)
2. **Provide credentials.** Put your Kraken keys in a gitignored `.env`:

   ```
   KRAKEN_API_KEY=...
   KRAKEN_API_SECRET=...        # base64, as Kraken issues it
   ```

   `.env` is gitignored — **never commit secrets**, never log keys (the broker
   redacts them).
3. **Opt in, in the config.** Set both flags in your `AppConfig` YAML:

   ```yaml
   mode: live
   live_enabled: true
   ```
4. **Run the hardening suite** and the full test suite — both must be green:

   ```bash
   .venv/bin/python -m pytest -q
   .venv/bin/ruff check trading_bot/
   .venv/bin/mypy trading_bot/
   ```
5. **Validate against a real-key sandbox.** This is the one remaining
   prerequisite and is **not done in-repo**: before any real order, exercise the
   private endpoints (AddOrder / OpenOrders / balances / fills) against a real
   Kraken key (ideally a low-limit, throwaway key) and confirm the
   venue-reported state matches what the engine requested — see *Proven vs
   pending*. **Do not skip this.**

Only after all five does a live `run` proceed to wire the live adapter.

---

## Pre-trade safety checklist

Before the first live order, confirm every item:

- [ ] **Risk limits set** in `RiskConfig`: `max_order` (largest single order),
      `max_position` (largest net exposure), `max_daily_loss` (the halt
      threshold). An unset limit is *unconstrained* — set them deliberately.
- [ ] **Kill-switch tested** — confirm the `RiskManager` kill-switch cancels open
      orders and halts new ones (covered offline by the hardening suite).
- [ ] **Reconcile-on-startup** — on start and after any disconnect the engine
      refetches open orders + balances + fills and reconciles local state;
      confirm it converges (proven offline).
- [ ] **Strategy paper-validated** — the exact strategy you intend to run has
      been validated in `mode: paper` over representative data and behaves as
      expected.
- [ ] **`starting_capital` set** to your real account value (anchors the KPI
      equity curve).
- [ ] **Credentials present and correct**, scoped to the minimum permissions
      needed, and a small position/order size for the first live run.

---

## Proven vs pending

| Concern | Proven offline (`tests/hardening/`) | Pending — needs a real-key sandbox |
|---|---|---|
| Reconciliation | Reconcile converges local state to broker-reported open orders / balances / fills after a disconnect | Real private-endpoint reads (OpenOrders / balances / fills) against a live key |
| Idempotency | **Engine-side** idempotency: a retried submit with the same client-order-id never double-submits locally | **Venue-level** idempotency token: Kraken honouring the client-order-id so a retry never creates a duplicate *at the venue* |
| Ambiguous failures | Ambiguous submit failures (timeout / unknown outcome) are surfaced, not silently assumed filled or failed | Real network-edge behaviour against the live API |
| Kill-switch | Kill-switch cancels open orders + halts new ones | Real cancel against the venue |
| Order placement | Full order lifecycle against the `PaperBroker` | **A real AddOrder has never been sent from this repo** |

The left column is what the offline suite demonstrates today. The right column is
the one remaining bridge to live — it requires a real key and is **out of scope
for this repository's automated tests** (which never hit a real venue).

---

## Disclaimers

- **Paper is the default.** Live is off by default and must be opted into
  deliberately, per the gates above.
- **No real order is ever sent by this repository's code paths or tests.** The
  live adapter is constructed only after every opt-in passes, and constructing it
  sends nothing.
- **Never risk money you cannot afford to lose.** Live trading uses real money;
  the authors provide no warranty. You are responsible for your keys, your risk
  limits, and your trades.
