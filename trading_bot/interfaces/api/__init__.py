"""trading_bot HTTP API — the unified dashboard FastAPI over the live engine(s).

The :mod:`trading_bot.interfaces.api` package exposes the managed strategies'
live state (positions, orders, PnL/KPI) plus a Server-Sent-Events stream over
HTTP, for the dashboard UI and any other HTTP client to consume. It is the
outermost ring of the hexagon: it reads the
:class:`~trading_bot.application.supervisor.StrategySupervisor` the factory
wired and renders its state out — it holds **no** business logic and never
places or cancels an order directly (see
:func:`~trading_bot.interfaces.api.app.create_dashboard_app`).

The single entrypoint is
:func:`~trading_bot.interfaces.api.app.create_dashboard_app`;
:func:`~trading_bot.interfaces.api.app.create_control_app` is kept as a thin
backward-compat alias of it.
"""

from __future__ import annotations

from trading_bot.interfaces.api.app import create_control_app, create_dashboard_app

__all__ = ["create_control_app", "create_dashboard_app"]
