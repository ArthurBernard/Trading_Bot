"""Web UI — the unified Jinja2 dashboard served by the FastAPI app.

This package ships the **static assets** of the dashboard: the Jinja2
``templates/`` (``base.html`` — the shared shell — plus one template per tab:
``overview.html``, ``strategies.html``, ``orders.html``, ``logs.html``, the
per-strategy ``strategy_detail.html`` and the ``strategies_new.html`` deploy
form, and the standalone ``login.html``) and ``static/`` (the
dependency-free ``format.js``, fonts, logo/favicon, and the vendored uPlot
chart assets). It holds **no Python logic** — the
:func:`~trading_bot.interfaces.api.app.create_dashboard_app` factory mounts
these directories (``StaticFiles`` at ``/static``, ``Jinja2Templates`` over
``templates/``) and registers the ``GET`` route per tab that renders its shell.

The dashboard is a **pure HTTP client** of the API (carried into the ADR)
--------------------------------------------------------------------------
Every page is a *shell only* — no supervisor data is ever rendered server-side.
Each page's script fetches ``/api/*`` over HTTP and live-updates from the
``/api/events`` SSE stream; it never reaches the application layer directly.
The UI can therefore only *observe* the engine(s) and, through the gated
control routes, start/stop a unit or switch its mode — it has no path to place
an order directly. Money arrives as exact :class:`~decimal.Decimal` strings and
is rendered **verbatim**; the JS never ``parseFloat``\\ s a money field (that
would reintroduce binary-float rounding the API took care to avoid).
"""

from __future__ import annotations

import pathlib

__all__ = ["UI_DIR", "STATIC_DIR", "TEMPLATES_DIR"]

#: The ``interfaces/ui`` package directory — resolved relative to this file so it
#: works both from a source checkout and an installed wheel (the templates/static
#: are shipped via ``[tool.setuptools.package-data]``).
UI_DIR = pathlib.Path(__file__).resolve().parent
#: The Jinja2 templates directory (``base.html`` shell + one template per tab).
TEMPLATES_DIR = UI_DIR / "templates"
#: The static-assets directory mounted at ``/static`` (``format.js``, fonts,
#: logo/favicon, and the vendored uPlot chart assets).
STATIC_DIR = UI_DIR / "static"
