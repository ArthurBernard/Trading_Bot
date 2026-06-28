# Trading_Bot

**Execution & orchestration layer of a three-repo trading stack.** Hexagonal,
async-first. *(Rewrite in progress — see status below.)*

![License](https://img.shields.io/github/license/ArthurBernard/Trading_Bot)

## The triptych

| Repo | Role |
|------|------|
| [**dccd**](https://github.com/ArthurBernard/Download_Crypto_Currencies_Data) | **Data** — multi-exchange market-data collection & storage (async, Parquet) |
| [**fynance**](https://github.com/ArthurBernard/Fynance) | **Research** — features, models, allocation, walk-forward backtest |
| **Trading_Bot** (this repo) | **Execution & orchestration** — run strategies live, route & manage orders, track positions / PnL / risk, and wire the other two together |

Trading_Bot is the part that *acts*: it takes data from dccd and signals from
fynance, and turns them into managed orders on real exchanges.

## Status

The project has been rewritten from scratch as a clean hexagonal, async-first
engine — harmonised with dccd. The MVP is in place: domain, transport, a Kraken
broker behind a multi-exchange port, a paper-trading broker, the order router,
strategy runner and a Typer CLI. (The pre-2026 implementation lives in git
history only.) Track progress in
[`doc/dev/07-roadmap.md`](doc/dev/07-roadmap.md).

**Design stance:** multi-exchange from day one (Kraken implemented first);
paper-trading by default, live behind an explicit opt-in; all money in `Decimal`;
reconcile-don't-assume; risk limits + kill-switch on every order.

## Install

```bash
git clone https://github.com/ArthurBernard/Trading_Bot.git
cd Trading_Bot
pip install -e ".[dev]"
```

Triptych integration (optional until the integration code lands):

```bash
pip install -e ".[dev,triptych]"                    # + fynance (PyPI)
pip install -e ../Download_Crypto_Currencies_Data   # dccd (editable)
```

## Develop

```bash
pytest                      # tests (network E2E excluded by default)
ruff check trading_bot/     # lint
mypy trading_bot/           # types
```

The repo follows a tooled dev loop (`/pick-task → /plan → /execute-leaf →
/finish-task → /release`); see [`CLAUDE.md`](CLAUDE.md) and
[`doc/dev/`](doc/dev/) for the full developer brief and conventions.

## Disclaimer

Do not risk money which you are afraid to lose. Use the trading bot at your own
risk; the authors assume no responsibility for your trading results. Read the
source code and make sure there are no undesirable behaviours.

## License

MIT — see [LICENSE.txt](LICENSE.txt).
