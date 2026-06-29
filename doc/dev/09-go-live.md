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
4. **Risk limits set** — a real-money live engine requires **all three**
   `RiskConfig` limits (`max_order`, `max_position`, `max_daily_loss`) to be
   set. `build_engine` raises `BrokerError` (naming the missing ones) if any is
   left `None` on the live path — an all-`None` config would trade with no
   size/exposure/daily-loss cap. Checked **after** credentials; paper and testnet
   (paper money) are exempt.
5. **CLI acknowledgement** — `trading-bot run --live` additionally requires
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
      threshold). All three are **now required on the live path** — `build_engine`
      refuses (a `BrokerError` naming the gaps) if any is left `None`, since an
      unset limit is *unconstrained*. The `max_daily_loss` breach is wired to halt
      the book (refuse new orders + cancel resting orders via the kill-switch).
- [ ] **Kill-switch tested** — confirm the `RiskManager` kill-switch cancels open
      orders and halts new ones (covered offline by the hardening suite). It is
      now auto-triggered on a `max_daily_loss` breach.
- [ ] **Reconcile on startup *and* reconnect** — on start the engine refetches open
      orders + balances + fills and reconciles before the first order; and on a live
      Kraken run the private fill WS (`KrakenPrivateWS` → `LiveFillStreamer`) re-runs
      `reconcile` on **every (re)connect**, so a disconnect re-syncs automatically.
      Both wired into `run_app`; convergence proven offline.
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
| Reconciliation | Reconcile converges local state to broker-reported open orders / balances / fills after a disconnect | — |
| **Private reads (read-only live ✓)** | — | **Validated read-only against real Kraken**: `OpenOrders` + `TradesHistory` (`fills`) returned + parsed; the private executions **WS** streamed a real snapshot. `balances` needs the key's *Query Funds* permission. **No order was sent.** |
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

### How a strategy like LS1 is wired (config + a thin generic adapter)

A concrete strategy lands as a **portfolio strategy** (`PortfolioStrategyConfig`),
not as bespoke engine code — and, per the project rule, **its files are local-only**:
they live under the **gitignored `strategies/`** tree (e.g. `strategies/ls1/`) and
are **never committed** to this engine repo (strategy IP stays outside the shareable
engine; only the tracked `strategies/example*/` templates are public).

- **The config** (local, e.g. `strategies/ls1/binance.yaml`): paper by default, the
  universe, `capital`, `gross_cap`, and a daily dccd data source.
- **The signal seam:** the config's `signal.ref` points at a thin **local** wrapper
  (e.g. `strategies.ls1.signal:ls1_portfolio_signal`) that binds a research oracle
  (e.g. `fynance_research.strategies.ls1_live:target_weights`) through the **generic**
  adapter [`as_portfolio_signal`](../../trading_bot/application/portfolio.py). That
  adapter — the only piece that ships in the engine — bridges any argument-free
  `() -> {pair: weight}` oracle to the `PortfolioSignalFn` contract
  (`(asof_ms, frames) -> {Symbol: weight}`): it normalises `"BTC-USDT"`-style keys to
  canonical `Symbol`s and weights to exact `Decimal`, and handles the
  `{...}`-or-`({...}, asof)` return shapes. It hardcodes no strategy specifics —
  `trading_bot` stays generic.
- **`fynance_research` is imported lazily** (inside the wrapper), so loading a config
  and resolving the ref need no research package — only *evaluating* the signal does.

### Data feed — dccd's Binance 1m store, resampled to daily

dccd's Binance store holds **1-minute** bars and `read` does not resample, so a
daily portfolio reads through a
[`ResamplingDccdClient`](../../trading_bot/application/data_provider.py) (1m → 1d,
OHLCV-correct, closed-day only — causal) wrapping the real `dccd.Client`. Sync the
10 `*-USDT` pairs first (`DEPLOY_LS1.md` §3); they land at
`~/data/arthurserver/binance/ohlc/<PAIR>/1m/`.

### Running a strategy's live tests (local)

The **generic** adapter and the portfolio engine are covered in the repo
(`trading_bot/tests/application/test_portfolio_*.py`). A **concrete strategy's** e2e
tests (real signal + real data + opt-in venue order tests) live **with the strategy**
under the gitignored `strategies/` tree, so run them by path — e.g. for LS1:

```bash
pip install -e ../fynance-research            # the research oracle (lazy-imported)
.venv/bin/python -m pytest strategies/ls1/test_e2e.py -m network -v
```

For a **Binance testnet** order round-trip, add a *testnet* key to a gitignored
`.env` (never a mainnet key) and `BINANCE_API_BASE=https://testnet.binance.vision`.
The testnet test places ONE tiny rebalance, reads `open_orders()`/`balances()` back,
asserts the legs match the intended deltas, **cancels** every leg, and refuses to run
against mainnet. (Kraken has no spot testnet — its live test is public-data +
PaperBroker, **no real order**.)

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
