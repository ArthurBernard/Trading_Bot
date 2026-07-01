"""trading_bot HTTP API — a **read-only** FastAPI over the live engine.

The :mod:`trading_bot.interfaces.api` package exposes the engine's live state
(positions, orders, PnL/KPI) plus a Server-Sent-Events stream over HTTP, for the
UI (leaf 02) and any other HTTP client to consume. It is the outermost ring of
the hexagon: it reads the :class:`~trading_bot.application.service_factory.Engine`
the factory wired and renders its state out — it holds **no** business logic and,
deliberately, **never mutates** the engine (no order is ever placed or cancelled
through this API; see :func:`~trading_bot.interfaces.api.app.create_app`).

The single entrypoint is :func:`~trading_bot.interfaces.api.app.create_app`.
"""

from __future__ import annotations

from trading_bot.interfaces.api.app import (
    create_app,
    create_control_app,
    create_dashboard_app,
)

__all__ = ["create_app", "create_control_app", "create_dashboard_app"]
