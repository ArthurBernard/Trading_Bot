"""trading_bot interfaces layer — the engine's outer edges (CLI, API, UI).

The outermost ring of the hexagon: it depends on the
:mod:`trading_bot.application` layer (and through it the inner layers) but
nothing depends on it. It turns human / network input into engine actions and
renders engine state back out, holding **no** business logic of its own — every
action is delegated to a use-case the
:func:`~trading_bot.application.service_factory.build_engine` factory wired.

* cli — the Typer command-line app (:mod:`trading_bot.interfaces.cli`), the
  console-script entrypoint (``trading-bot``). Starts minimal (a ``version``
  command) and grows the real start/stop/status/KPI commands in later leaves.
* api — a **read-only** FastAPI over the engine
  (:mod:`trading_bot.interfaces.api`): positions / orders / PnL+KPI as JSON
  (money as Decimal strings) plus an SSE event stream. No endpoint ever places
  or cancels an order.

The Jinja2 dashboard (``ui``) lands later.
"""

from __future__ import annotations
