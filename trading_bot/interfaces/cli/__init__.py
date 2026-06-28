"""trading_bot CLI — the Typer command-line interface.

The ``trading-bot`` console script. The Typer application lives in
:mod:`trading_bot.interfaces.cli.main` and is exposed here as :data:`app` so the
console-script target ``trading_bot.interfaces.cli.main:app`` and importers alike
resolve a single object.

This leaf ships only the skeleton (a ``version`` command); the real
start/stop/status/KPI commands land in the next CLI leaf.
"""

from __future__ import annotations

from trading_bot.interfaces.cli.main import app

__all__ = ["app"]
