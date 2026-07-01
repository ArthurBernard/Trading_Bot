# Deploy — running the dashboard under systemd

How to keep `trading_bot` running as a supervised process (like dccd's `dccd start`
+ `dccd.service`), serving the **unified dashboard** — one UI across Overview /
Strategies / Orders / PnL / Logs that is *also* the control plane (start/stop, mode
switching, deploy/remove).

> **Paper by default.** The dashboard boots every declared strategy in its configured
> mode — **paper** unless you say otherwise. It **never trades real money by merely
> starting**: going live is a deliberate act from the dashboard (a typed
> confirmation) and still requires the `live_enabled` + credentials + risk-limit
> gates (`09-go-live.md`).

## What runs — the single `dashboard` command

`trading-bot dashboard` is the long-running process the systemd unit runs:

- builds a `StrategySupervisor` — **one engine per strategy** (own broker/mode), so
  strategies can be started/stopped and switched **paper / testnet / live**
  independently;
- **starts every declared strategy** (each comes online *restored* — a paper unit's
  persisted book replayed into its engine — and immediately controllable);
- serves the unified dashboard over HTTP (loopback by default), reading/rewriting a
  **persistent manifest** (`configs/dashboard.yaml` by default, or `-c <file>`) so
  deployments survive a restart.

```bash
# the primary command (paper by default):
trading-bot dashboard                       # dashboard on http://127.0.0.1:8000
trading-bot dashboard -c config.yaml        # over an explicit manifest
trading-bot dashboard --read-only           # observe only (control mutations → 403)
```

The dashboard lists each strategy with a **mode selector** and **start/stop**
buttons, an **Orders/Fills** history page (filterable by crypto / exchange /
strategy), a **PnL** chart and a live **Logs** activity feed. Switching a strategy
to **live** prompts for a typed `I UNDERSTAND` and sends `confirm: true`; the server
refuses live without it (`403`).

### Related commands

- **`trading-bot serve`** — a lightweight **read-only** alias of the dashboard
  (`dashboard --read-only`): observe positions / orders / fills / PnL, no control. It
  does not start strategies or rewrite a manifest.
- **`trading-bot start [--serve]`** — the **headless scheduler daemon**: it steps the
  running strategies on an `--interval SECONDS` (idempotent ticks) or `--cron
  "m h * * *"`, and with `--serve` *also* serves the same unified dashboard. Use it
  when you want scheduled re-evaluation on top of the dashboard.

```bash
trading-bot start -c config.yaml --cron "5 0 * * *"   # re-evaluate daily at 00:05
trading-bot start -c config.yaml --serve              # + serve the dashboard
```

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

The dashboard binds **loopback** (`127.0.0.1`) by default — it can change what
trades. Two ways to reach it remotely:

**1. SSH tunnel (simplest, most secure).** Keep it loopback, forward a port:

```bash
ssh -L 8000:127.0.0.1:8000 your-host    # then open http://localhost:8000
```

**2. Direct access with a token (like dccd).** Set a token and bind a reachable
interface; the dashboard then refuses to bind non-loopback **without** a token:

```bash
export TRADING_BOT_UI_TOKEN="$(openssl rand -hex 24)"   # a strong secret
trading-bot dashboard -c config.yaml \
    --host 0.0.0.0 --port 8000 --token "$TRADING_BOT_UI_TOKEN"
```

With a token set, the dashboard requires a **login**: `/login` exchanges the token
for an HttpOnly session cookie; every other route is gated (401 for `/api/*`,
redirect to `/login` for pages), login attempts are rate-limited, and `/api/*` also
accepts a `Bearer <token>` header or `?token=` query for scripts. In the systemd
unit, put `TRADING_BOT_UI_TOKEN=…` in the `EnvironmentFile` (0600) and add
`--token "$TRADING_BOT_UI_TOKEN"` (or rely on the env var — `dashboard --token`
reads `TRADING_BOT_UI_TOKEN`) to `ExecStart`. The `--host`/`--port`/`--token` flags
mirror `start`'s `--serve-host`/`--serve-port`/`--serve-token` when you run the
scheduler daemon instead.

> **Put it behind HTTPS.** The token + session protect access, but run the daemon
> behind a TLS reverse proxy (Caddy / nginx / a Tailscale serve) so credentials and
> the session cookie are encrypted in transit. The cookie is marked `Secure`
> automatically when the request arrives over HTTPS (incl. `X-Forwarded-Proto`).

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
