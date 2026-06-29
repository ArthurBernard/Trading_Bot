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

## Running LS1 (the first real portfolio strategy)

**LS1** is the validated long/short crypto book from the research repo — a daily
multi-asset strategy over a 10-coin Binance USDT universe (trend core on BTC/ETH
+ a cross-sectional momentum overlay, hard-capped at 2× gross). Its full dossier
— universe, fees, the exact signal recipe, sizing, rebalance and risk rules, and
the live signal API — is **[`../fynance-research/DEPLOY_LS1.md`](../../../fynance-research/DEPLOY_LS1.md)**.
Read it before running LS1. `trading_bot` *executes* LS1; it does not define it.

### How LS1 is wired (config + a thin generic adapter — no engine code)

LS1 lands as a **portfolio strategy** (`PortfolioStrategyConfig`), not as bespoke
engine code:

- **The config:** [`configs/ls1.yaml`](../../configs/ls1.yaml) — paper by default,
  the 10-coin universe, `capital`, `gross_cap: 2`, and a daily Binance data source
  (`exchange: binance`, `span: 86400`).
- **The signal seam:** the config's `signal.ref` points at
  `examples.ls1_signal:ls1_portfolio_signal`
  ([`examples/ls1_signal.py`](../../examples/ls1_signal.py)) — a thin wrapper that
  binds the research oracle `fynance_research.strategies.ls1_live:target_weights`
  through the **generic** adapter
  [`as_portfolio_signal`](../../trading_bot/application/portfolio.py). The adapter
  bridges any argument-free `() -> {pair: weight}` oracle to the engine's
  `PortfolioSignalFn` contract (`(asof_ms, frames) -> {Symbol: weight}`): it
  normalises `"BTC-USDT"`-style keys to canonical `Symbol`s and weights to exact
  `Decimal`, and handles the `{...}`-or-`({...}, asof)` return shapes. It hardcodes
  no LS1 specifics — `trading_bot` stays generic.
- **`fynance_research` is imported lazily** (inside the signal), so loading the
  config and resolving the ref need no research package — only *evaluating* the
  signal at run time does.

### Data feed — dccd's Binance 1m store, resampled to daily

dccd's Binance store holds **1-minute** bars and `read` does not resample, so a
daily portfolio reads through a
[`ResamplingDccdClient`](../../trading_bot/application/data_provider.py) (1m → 1d,
OHLCV-correct, closed-day only — causal) wrapping the real `dccd.Client`. Sync the
10 `*-USDT` pairs first (`DEPLOY_LS1.md` §3); they land at
`~/data/arthurserver/binance/ohlc/<PAIR>/1m/`.

### Prerequisites and how to run the LS1 tests

The portfolio path is proven end-to-end on **real** dccd Binance bars (with a
deterministic weight vector) in
[`trading_bot/tests/application/test_ls1_e2e.py`](../../trading_bot/tests/application/test_ls1_e2e.py).
The LS1-specific and Binance-testnet checks live in the same file, gated:

```bash
# Real-dccd portfolio e2e (runs whenever the Binance store is present):
.venv/bin/python -m pytest trading_bot/tests/application/test_ls1_e2e.py -m network -v

# LS1-real e2e — needs the research package:
pip install -e ../fynance-research
.venv/bin/python -m pytest \
  trading_bot/tests/application/test_ls1_e2e.py::test_ls1_real_e2e -m network -v

# Binance testnet rebalance — needs a *testnet* key + the testnet base URL
# (in a gitignored .env; never a mainnet key):
export BINANCE_API_KEY=...  BINANCE_API_SECRET=...
export BINANCE_API_BASE=https://testnet.binance.vision
.venv/bin/python -m pytest \
  trading_bot/tests/application/test_ls1_e2e.py::test_binance_testnet_rebalance -m network -v
```

The testnet test places ONE rebalance with a tiny capital, reads the venue's
`open_orders()`/`balances()` back, asserts the placed legs match the intended
deltas, then **cancels** every leg — and refuses to run against mainnet.

### Testnet on the engine path (the safe, low-ceremony way)

To live-test orders through the **engine** (`trading-bot run` / `build_engine`)
rather than the test, set **`testnet: true`** on the broker — do **not** flip
`live_enabled` or `BINANCE_API_BASE`:

```yaml
mode: live
# live_enabled NOT needed — testnet cannot reach mainnet (paper money)
brokers:
  - { name: binance, exchange: binance, testnet: true }
```

With testnet credentials in `.env`, the factory builds a `BinanceBroker`
**hard-pinned** to `testnet.binance.vision` (the URL is forced from the flag, so a
stray `BINANCE_API_BASE` pointing at mainnet is ignored) — it is structurally
incapable of trading real money, which is why it is exempt from the `live_enabled`
opt-in. Only Binance qualifies; `testnet: true` on Kraken raises (no public spot
testnet). **Real mainnet** still requires `live_enabled: true` + a real key (above).

### Going live with LS1

Live LS1 follows the **same** gates as any live run (above): `mode: live` +
`live_enabled: true` + Binance credentials + the real-key sandbox step. LS1's
`gross_cap` lives in the signal; add per-coin `max_order` / `max_position` and a
`max_daily_loss` halt in `RiskConfig` before the first live order. Note LS1's
documented net-long bias and unmodelled funding (`DEPLOY_LS1.md` §7) — budget for
them separately.

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
