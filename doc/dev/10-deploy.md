# Deploy — running the daemon under systemd

How to keep `trading_bot` running as a supervised daemon (like dccd's `dccd start`
+ `dccd.service`), with the control dashboard for start/stop + mode switching.

> **Paper by default.** The daemon boots every declared strategy in its configured
> mode — **paper** unless you say otherwise. It **never trades real money by merely
> starting**: going live is a deliberate act from the control dashboard (a typed
> confirmation) and still requires the `live_enabled` + credentials + risk-limit
> gates (`09-go-live.md`).

## What the daemon is

`trading-bot start` is the long-running process:

- builds a `StrategySupervisor` — **one engine per strategy** (own broker/mode), so
  strategies can be started/stopped and switched **paper / testnet / live**
  independently;
- starts every declared strategy and **re-evaluates** them on a schedule
  (`--interval SECONDS`, idempotent ticks, or `--cron "m h * * *"`);
- with `--serve`, serves the **control dashboard** over HTTP (loopback by default).

```bash
# foreground, for a quick look (paper):
trading-bot start -c config.yaml --serve            # dashboard on http://127.0.0.1:8000
trading-bot start -c config.yaml --cron "5 0 * * *" # re-evaluate daily at 00:05
```

The dashboard lists each strategy with a **mode selector** and **start/stop**
buttons. Switching a strategy to **live** prompts for a typed `I UNDERSTAND` and
sends `confirm: true`; the server refuses live without it (`403`).

## systemd (pyenv)

The repo ships [`deploy/trading-bot.service`](../../deploy/trading-bot.service) — a
unit modelled on dccd's, using a **pyenv**-managed console script (this machine
prefers pyenv over a project venv). Its header has the full install recipe; the
essentials:

```bash
# 1) a pyenv interpreter/venv with trading_bot installed:
pyenv install -s 3.12.13
pyenv virtualenv 3.12.13 trading-bot
~/.pyenv/versions/trading-bot/bin/pip install -e ~/dev/Trading_Bot[daemon]

# 2) config + credentials (never world-readable):
sudo install -d -m 0750 /etc/trading-bot
sudo cp config.yaml      /etc/trading-bot/config.yaml         # paper by default
sudo install -m 0600 .env /etc/trading-bot/trading-bot.env    # KRAKEN_/BINANCE_ keys

# 3) edit ExecStart (your pyenv path) + User= in the unit, then:
sudo cp deploy/trading-bot.service /etc/systemd/system/trading-bot.service
sudo systemctl daemon-reload && sudo systemctl enable --now trading-bot
journalctl -u trading-bot -f                                   # watch it
```

`Restart=on-failure` keeps it alive across crashes; the engine is **restart-safe**
(on each (re)start it restores the router's dedup map from the store and reconciles
to the venue), so a restart converges state rather than duplicating orders.

### Reaching the dashboard

The control UI binds **loopback** (`127.0.0.1`) on purpose — it can change what
trades. Reach it over an SSH tunnel, never expose it publicly:

```bash
ssh -L 8000:127.0.0.1:8000 your-host    # then open http://localhost:8000
```

### Operational notes

- **Logs**: `journalctl -u trading-bot [-f]` (the daemon writes no log files of its
  own).
- **State / DB**: point `storage.db_path` at `/var/lib/trading-bot/…` (the unit's
  `StateDirectory`, which is the only writable path under the hardening directives).
- **Stop / restart**: `systemctl stop|restart trading-bot` — the daemon drains and
  shuts every engine down gracefully within `TimeoutStopSec`.
- **Going live**: flip a strategy to `live` from the dashboard (typed confirmation),
  with credentials present and `RiskConfig` limits set — see `09-go-live.md`. Until
  you do, every strategy stays in paper/testnet (no real order).
