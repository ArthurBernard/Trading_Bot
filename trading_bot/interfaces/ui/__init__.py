"""Web UI — a read-only Jinja2 dashboard served by the FastAPI app.

This package ships the **static assets** of the dashboard: the Jinja2
``templates/`` (the server-rendered shell) and ``static/`` (the dependency-free
``app.js`` + ``style.css``). It holds **no Python logic** — the
:func:`~trading_bot.interfaces.api.app.create_app` factory mounts these
directories (``StaticFiles`` at ``/static``, ``Jinja2Templates`` over
``templates/``) and registers the ``GET /`` route that renders the shell.

The dashboard is a **pure HTTP client** of the leaf-01 API (carried into the ADR)
-----------------------------------------------------------------------------------
The served HTML is a *shell only* — no engine data is ever rendered server-side.
``app.js`` fetches ``/api/positions``, ``/api/orders`` and ``/api/kpi`` over HTTP
and live-updates from the ``/api/events`` SSE stream; it never reaches the
application layer. The UI can therefore only *observe* the engine — like the API
it sits behind, it is **read-only** and has no path to place an order. Money
arrives as exact :class:`~decimal.Decimal` strings and is rendered **verbatim**;
the JS never ``parseFloat``\\ s a money field (that would reintroduce binary-float
rounding the API took care to avoid).
"""

from __future__ import annotations

import pathlib

__all__ = ["UI_DIR", "STATIC_DIR", "TEMPLATES_DIR"]

#: The ``interfaces/ui`` package directory — resolved relative to this file so it
#: works both from a source checkout and an installed wheel (the templates/static
#: are shipped via ``[tool.setuptools.package-data]``).
UI_DIR = pathlib.Path(__file__).resolve().parent
#: The Jinja2 templates directory (``dashboard.html`` + future pages).
TEMPLATES_DIR = UI_DIR / "templates"
#: The static-assets directory mounted at ``/static`` (``app.js`` + ``style.css``).
STATIC_DIR = UI_DIR / "static"
