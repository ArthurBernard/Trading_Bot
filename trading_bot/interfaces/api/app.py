"""FastAPI application — a **read-only** HTTP view of the live engine.

``create_app(engine)`` builds a :class:`fastapi.FastAPI` that renders the
:class:`~trading_bot.application.service_factory.Engine`'s live state out over
HTTP: positions, tracked orders and PnL/KPI as JSON, plus a Server-Sent-Events
stream of order/fill/log events fed by the engine's
:class:`~trading_bot.application.events.EventBus`. The UI (leaf 02) is a pure
HTTP client of this API.

Read-only — a hard invariant (carried into the ADR)
---------------------------------------------------
**Every endpoint is a GET and no endpoint mutates the engine.** There is
deliberately *no* route that places, amends or cancels an order: the write path
(:class:`~trading_bot.application.order_router.OrderRouter`) is reachable only
in-process by the strategy runner, never from the network. A web client can
*observe* the engine — it can never *trade* through it. This keeps the only
money-moving surface (order submission) off the HTTP boundary entirely, so a
compromised or misused web client cannot place an order. The absence of a POST
order route is the invariant; a POST to a plausible order path returns ``405``
(method not allowed) because only GET is registered for it.

Money is serialized as Decimal **strings**, never floats (carried into the ADR)
-------------------------------------------------------------------------------
A price of ``0.1`` must appear in the JSON as the string ``"0.1"`` — exact, with
no binary-float rounding. JSON has no decimal type and Python's ``json`` renders
a :class:`~decimal.Decimal` via ``float()`` by default (lossy). To guarantee
exactness, this module **never lets a Decimal reach the float path**: the route
handlers build plain dicts whose money fields are already ``str(Decimal)`` (and
``None`` for an absent optional, rendered as JSON ``null``), and responses go out
through :class:`_DecimalJSONResponse`, whose encoder stringifies any stray
:class:`~decimal.Decimal` as a defence in depth. The KPI *ratios* (Sharpe,
Sortino, max-drawdown, Calmar) are statistical estimators, not money, so they go
out as JSON numbers (floats are fine there).

SSE — mirrors dccd (carried into the ADR)
-----------------------------------------
``GET /api/events`` registers its own :class:`asyncio.Queue` on the bus via
:meth:`~trading_bot.application.events.EventBus.add_queue`, an async generator
``await``\\ s the queue and yields ``data: <json>\\n\\n`` frames (each event
serialized with money as strings and tagged with a ``type`` discriminator), and
:meth:`~trading_bot.application.events.EventBus.remove_queue` runs in a
``finally`` so the queue is always unregistered on disconnect — exactly dccd's
``/api/events`` shape.

The dashboard UI — a pure HTTP client mounted on the same app (carried into the ADR)
------------------------------------------------------------------------------------
``create_app`` also mounts the read-only web dashboard (leaf 02): ``StaticFiles``
at ``/static`` over :data:`~trading_bot.interfaces.ui.STATIC_DIR`, a
:class:`~fastapi.templating.Jinja2Templates` over
:data:`~trading_bot.interfaces.ui.TEMPLATES_DIR`, and a single ``GET /`` that
renders ``dashboard.html`` — a **shell** carrying only the package version and the
engine ``mode`` (no engine data is rendered server-side). The page's ``app.js``
fetches ``/api/positions|orders|kpi`` and live-updates from ``/api/events``, so the
UI is a **pure HTTP client** of this API: it shares the API's read-only guarantee
and has no path to place an order. The directories are resolved from the installed
package (shipped via ``[tool.setuptools.package-data]``), and the mount is guarded
on their existence so the API still builds if assets are absent.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import trading_bot
from trading_bot.application.events import (
    Event,
    FillEvent,
    LogEvent,
    OrderEvent,
)
from trading_bot.interfaces.ui import STATIC_DIR, TEMPLATES_DIR

if TYPE_CHECKING:
    from trading_bot.application.service_factory import Engine
    from trading_bot.application.supervisor import (
        StrategyStatus,
        StrategySupervisor,
    )
    from trading_bot.domain.fill import Fill
    from trading_bot.domain.order import Order
    from trading_bot.domain.position import Position

__all__ = ["create_app", "create_control_app"]


# ---------------------------------------------------------------------------
# Decimal-as-string JSON — the money-exactness crux
# ---------------------------------------------------------------------------

def _money_str(value: Decimal | None) -> str | None:
    """Render a money :class:`~decimal.Decimal` as an exact string (``None`` passes).

    ``str(Decimal("0.1"))`` is ``"0.1"`` — exact, with no float round-trip. A
    ``None`` (an absent optional money field) passes through so it serializes as
    JSON ``null``.
    """
    return None if value is None else str(value)


def _default(obj: Any) -> str:
    """``json`` encoder hook: stringify a :class:`~decimal.Decimal` (defence in depth).

    The route handlers already stringify money before it reaches the encoder, so
    in practice no bare ``Decimal`` arrives here. This hook is the safety net: if
    one ever slips through, it is rendered as ``str(Decimal)`` (exact) rather than
    via ``float()`` (lossy) or raising.
    """
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"object of type {type(obj).__name__!r} is not JSON serializable")


class _DecimalJSONResponse(JSONResponse):
    """A :class:`~fastapi.responses.JSONResponse` that renders Decimals as strings.

    Replaces FastAPI's default encoder so *any* :class:`~decimal.Decimal` in a
    response body is serialized as an exact string (``str(Decimal)``), never as a
    lossy ``float``. Used as the app-wide ``default_response_class`` so every
    endpoint inherits the guarantee.
    """

    def render(self, content: Any) -> bytes:
        """Serialize *content* to UTF-8 JSON bytes with the Decimal-as-string hook."""
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            default=_default,
        ).encode("utf-8")


# ---------------------------------------------------------------------------
# Serialization — engine objects -> JSON-ready dicts (money already stringified)
# ---------------------------------------------------------------------------

def _position_dict(position: Position) -> dict[str, Any]:
    """Render a :class:`~trading_bot.domain.position.Position` as a JSON-ready dict.

    Money fields (``net_qty``, ``avg_entry_price``, ``realised_pnl``,
    ``fees_paid``) are exact :class:`~decimal.Decimal` strings; ``avg_entry_price``
    is ``None`` (JSON ``null``) when the position is flat.
    """
    return {
        "instrument": str(position.instrument),
        "net_qty": _money_str(position.net_qty),
        "avg_entry_price": _money_str(position.avg_entry_price),
        "realised_pnl": _money_str(position.realised_pnl),
        "fees_paid": _money_str(position.fees_paid),
    }


def _order_dict(order: Order) -> dict[str, Any]:
    """Render an :class:`~trading_bot.domain.order.Order` as a JSON-ready dict.

    Enums (``side``, ``type``, ``status``) serialize by their ``.value``; money
    fields (``qty``, ``limit_price``, ``stop_price``, ``filled_qty``,
    ``avg_fill_price``) as exact Decimal strings, with ``null`` for an absent
    optional price / a not-yet-filled average.
    """
    return {
        "client_order_id": order.client_order_id,
        "venue_order_id": order.venue_order_id,
        "instrument": str(order.instrument),
        "side": order.side.value,
        "type": order.type.value,
        "qty": _money_str(order.qty),
        "limit_price": _money_str(order.limit_price),
        "stop_price": _money_str(order.stop_price),
        "status": order.status.value,
        "filled_qty": _money_str(order.filled_qty),
        "avg_fill_price": _money_str(order.avg_fill_price),
    }


def _fill_dict(fill: Fill) -> dict[str, Any]:
    """Render a :class:`~trading_bot.domain.fill.Fill` as a JSON-ready dict.

    Money fields (``qty``, ``price``, ``fee``) as exact Decimal strings; ``side``
    by its ``.value``; ``ts`` as the integer milliseconds it already is.
    """
    return {
        "fill_id": fill.fill_id,
        "client_order_id": fill.client_order_id,
        "instrument": str(fill.instrument),
        "side": fill.side.value,
        "qty": _money_str(fill.qty),
        "price": _money_str(fill.price),
        "fee": _money_str(fill.fee),
        "ts": fill.ts,
    }


def _safe_ratio(compute: Callable[[], float]) -> float:
    """Evaluate a KPI ratio, returning ``0.0`` when it is undefined on this curve.

    The fynance-backed ratio estimators can both *raise* and *return a
    non-finite value* on some real equity curves the fill-driven
    :class:`~trading_bot.application.performance_service.PerformanceService`
    produces:

    * they **raise** a :class:`ValueError` when the curve is degenerate (e.g. a
      curve that starts at / crosses zero — fynance's "initial value cannot be
      null" / "must be of the same sign");
    * they **return ``inf`` / ``nan``** when a ratio's denominator is zero on an
      otherwise valid curve — e.g. a *monotonically rising* curve has zero
      drawdown, so Calmar (return / max-drawdown) and Sortino (excess /
      downside-deviation) are ``inf``. With a strictly-positive
      ``starting_capital`` (the config default) this is now the *common* shape
      for a winning run, where the old ``v0 = 0`` curve would instead have made
      fynance raise.

    A read-only KPI view must stay 200 + JSON-numeric in both cases (a bare
    ``inf`` / ``nan`` is not valid JSON and serializes to ``null``). So this
    reports ``0.0`` for a raised *and* a non-finite ratio — the same "undefined
    estimator → 0.0" convention the service uses for a too-short series.
    """
    try:
        value = compute()
    except (ValueError, ZeroDivisionError, ArithmeticError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _event_dict(event: Event) -> dict[str, Any]:
    """Serialize a bus :class:`~trading_bot.application.events.Event` for SSE.

    Tags the payload with a ``type`` discriminator and embeds the event's domain
    object rendered with money as Decimal strings: an
    :class:`~trading_bot.application.events.OrderEvent` -> the order dict, a
    :class:`~trading_bot.application.events.FillEvent` -> the fill dict, a
    :class:`~trading_bot.application.events.LogEvent` -> ``{message, level}``.
    """
    if isinstance(event, OrderEvent):
        return {"type": "order", "order": _order_dict(event.order)}
    if isinstance(event, FillEvent):
        return {"type": "fill", "fill": _fill_dict(event.fill)}
    if isinstance(event, LogEvent):
        return {"type": "log", "message": event.message, "level": event.level}
    # Defensive: an unknown event type still streams a typed, JSON-safe frame.
    return {"type": "unknown", "repr": repr(event)}


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(engine: Engine) -> FastAPI:
    """Build the read-only FastAPI over a wired :class:`Engine`.

    Stores ``engine`` on ``app.state`` and registers the read-only GET endpoints
    (``/api/health``, ``/api/positions``, ``/api/orders``, ``/api/kpi``) plus the
    SSE stream (``/api/events``). Every response renders money as an exact
    :class:`~decimal.Decimal` string (see the module docstring). **No** endpoint
    mutates the engine — there is deliberately no route to place or cancel an
    order.

    Parameters
    ----------
    engine : Engine
        The fully-wired engine to expose. Read through ``app.state.engine`` by the
        handlers, so the wiring is explicit and the app is testable with a paper
        engine.

    Returns
    -------
    FastAPI
        The configured application — pass it to a server (uvicorn) or to
        :class:`fastapi.testclient.TestClient`.

    """
    app = FastAPI(
        title="trading_bot API",
        summary="Read-only HTTP view of the live trading engine.",
        default_response_class=_DecimalJSONResponse,
    )
    app.state.engine = engine

    def _engine(request: Request) -> Engine:
        """Read the wired engine off ``app.state`` (explicit, testable access)."""
        return request.app.state.engine  # type: ignore[no-any-return]

    # -- UI: dashboard shell + static assets --------------------------------- #
    # Mount the read-only web dashboard on the same app. The page is a *shell*
    # (version + mode only); all engine data is fetched client-side from /api/*,
    # so the UI is a pure HTTP client of this API (read-only, no order path).
    # Guarded on the dirs existing so the API still builds without the assets.
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = (
        Jinja2Templates(directory=str(TEMPLATES_DIR))
        if TEMPLATES_DIR.is_dir()
        else None
    )

    if templates is not None:

        @app.get("/", response_class=HTMLResponse)
        async def dashboard(request: Request) -> Any:
            """Render the read-only dashboard shell (no engine data server-side).

            Returns ``dashboard.html`` carrying only the package version and the
            engine ``mode`` (for the header badge). The page's ``app.js`` fetches
            ``/api/positions|orders|kpi`` and live-updates from ``/api/events`` —
            the UI never renders engine state server-side and never mutates it.
            """
            eng = _engine(request)
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                {
                    "version": trading_bot.__version__,
                    "mode": eng.config.mode,
                },
            )

    # -- Health -------------------------------------------------------------- #

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        """Liveness + a snapshot of what the engine is configured to run."""
        eng = _engine(request)
        return {
            "status": "ok",
            "mode": eng.config.mode,
            "strategies": len(eng.config.strategies),
        }

    # -- Positions ----------------------------------------------------------- #

    @app.get("/api/positions")
    async def positions(request: Request) -> list[dict[str, Any]]:
        """Live net positions per instrument (money as Decimal strings)."""
        eng = _engine(request)
        return [
            _position_dict(position)
            for position in eng.tracker.all_positions().values()
        ]

    # -- Orders -------------------------------------------------------------- #

    @app.get("/api/orders")
    async def orders(request: Request) -> list[dict[str, Any]]:
        """Every order the router has tracked (enums by value; money as strings)."""
        eng = _engine(request)
        return [_order_dict(order) for order in eng.router.tracked_orders().values()]

    # -- KPI ----------------------------------------------------------------- #

    @app.get("/api/kpi")
    async def kpi(request: Request) -> dict[str, Any]:
        """Aggregate PnL/KPI: money as Decimal strings, ratios as JSON numbers."""
        perf = _engine(request).perf
        equity = perf.equity_curve()
        equity_end = equity[-1] if equity else None
        return {
            "realised_pnl": _money_str(perf.realised_pnl()),
            "fees_paid": _money_str(perf.fees_paid()),
            "equity_end": _money_str(equity_end),
            "sharpe": _safe_ratio(perf.sharpe),
            "sortino": _safe_ratio(perf.sortino),
            "max_drawdown": _safe_ratio(perf.max_drawdown),
            "calmar": _safe_ratio(perf.calmar),
        }

    # -- SSE events ---------------------------------------------------------- #

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        """Server-Sent-Events stream of order/fill/log events from the bus.

        Registers a fresh queue on the engine's
        :class:`~trading_bot.application.events.EventBus`, yields each event as a
        ``data: <json>\\n\\n`` frame (money as Decimal strings, tagged with a
        ``type``), and unregisters the queue in a ``finally`` on disconnect.
        """
        bus = _engine(request).bus
        queue = bus.add_queue()

        async def _generator() -> Any:
            try:
                # Flush an immediate comment so the client's EventSource leaves
                # "connecting" without waiting for the first real event (mirrors
                # dccd, where a buffering middleware otherwise stalls the start).
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        # Bounded wait so the loop periodically wakes to re-check
                        # disconnection (and so a hung consumer cannot pin the
                        # queue forever); on timeout, send an SSE heartbeat
                        # comment. Mirrors dccd's /api/events.
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield (
                            f"data: {json.dumps(_event_dict(event), default=_default)}\n\n"
                        )
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                bus.remove_queue(queue)

        return StreamingResponse(_generator(), media_type="text/event-stream")

    return app


# ---------------------------------------------------------------------------
# The control app — the daemon's read+write dashboard over a StrategySupervisor
# ---------------------------------------------------------------------------


#: Valid deployment modes the control API accepts.
_CONTROL_MODES = ("paper", "testnet", "live")

#: Browser session cookie set on a successful /login (opaque, HttpOnly).
_SESSION_COOKIE = "tb_session"
#: How long a control session stays valid (seconds).
_SESSION_TTL_SECONDS = 12 * 3600
#: Login attempts per minute per client (brute-force throttle).
_LOGIN_RATE_PER_MIN = 10
#: Path prefixes reachable without a session (the login flow + assets).
_OPEN_PREFIXES = ("/login", "/logout", "/static")


class _ModeBody(BaseModel):
    """Request body for ``POST /api/strategies/{name}/mode``.

    ``confirm`` must be ``true`` to switch to ``live`` (real money) — the
    deliberate acknowledgement; the endpoint refuses live without it.
    """

    mode: str
    confirm: bool = False


def _status_dict(status: StrategyStatus) -> dict[str, Any]:
    """Render a :class:`StrategyStatus` for JSON (money as exact Decimal string)."""
    return {
        "name": status.name,
        "kind": status.kind,
        "exchange": status.exchange,
        "mode": status.mode,
        "running": status.running,
        "realised_pnl": (
            str(status.realised_pnl) if status.realised_pnl is not None else None
        ),
        "open_orders": status.open_orders,
    }


def create_control_app(
    supervisor: StrategySupervisor, *, auth_token: str | None = None
) -> FastAPI:
    """Build the daemon's **control** FastAPI over a :class:`StrategySupervisor`.

    Unlike :func:`create_app` (a *read-only* view of one engine), this app is the
    **control plane**: it lists the managed strategies and lets a client **start /
    stop** them and **switch mode** (paper / testnet / live). It binds **loopback**
    by default; to reach it remotely, either tunnel (SSH) or set ``auth_token``.

    **Real money is gated.** Switching a strategy to ``live`` requires
    ``confirm: true`` in the request body — the deliberate acknowledgement the UI
    obtains (a typed confirmation); otherwise the endpoint returns **403** and
    nothing changes. The factory's credential + risk-limit gates still apply when a
    live unit is actually started.

    **Authentication (for remote exposure).** With ``auth_token`` set, the app gates
    every route behind a token login: ``/login`` exchanges the token for an
    HttpOnly session cookie, an auth-guard middleware then refuses unauthenticated
    requests (``401`` for ``/api/*``; redirect to ``/login`` for pages), and login
    attempts are rate-limited. ``/api/*`` also accepts a ``Bearer <token>`` header
    or ``?token=`` query (for non-browser clients). With ``auth_token`` ``None`` (the
    default) there is **no** auth — only safe behind loopback / an SSH tunnel.

    Parameters
    ----------
    supervisor : StrategySupervisor
        The supervisor to expose, read+controlled through ``app.state.supervisor``.
    auth_token : str or None, optional
        When set, require this token to log in (enables the auth guard). ``None``
        (default) leaves the app unauthenticated — loopback / tunnel only.

    Returns
    -------
    FastAPI
        The control application (serve via uvicorn, or a ``TestClient``).

    """
    from fastapi import HTTPException

    from trading_bot.domain.errors import (
        BrokerError,
        ConfigError,
        LiveTradingNotEnabled,
    )

    app = FastAPI(
        title="trading_bot control",
        summary="Supervise + control the declared strategies.",
        default_response_class=_DecimalJSONResponse,
    )
    app.state.supervisor = supervisor
    app.state.auth_enabled = bool(auth_token)

    def _sup(request: Request) -> StrategySupervisor:
        return request.app.state.supervisor  # type: ignore[no-any-return]

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = (
        Jinja2Templates(directory=str(TEMPLATES_DIR))
        if TEMPLATES_DIR.is_dir()
        else None
    )

    if templates is not None:

        @app.get("/", response_class=HTMLResponse)
        async def control_dashboard(request: Request) -> Any:
            """Render the control dashboard shell (data fetched client-side)."""
            return templates.TemplateResponse(
                request,
                "control.html",
                {
                    "version": trading_bot.__version__,
                    "auth": request.app.state.auth_enabled,
                },
            )

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        names = _sup(request).names()
        running = sum(1 for s in _sup(request).status() if s.running)
        return {"status": "ok", "strategies": len(names), "running": running}

    @app.get("/api/strategies")
    async def strategies(request: Request) -> list[dict[str, Any]]:
        """List every managed strategy with its mode / running / PnL."""
        return [_status_dict(s) for s in _sup(request).status()]

    @app.post("/api/strategies/{name}/start")
    async def start_strategy(name: str, request: Request) -> dict[str, Any]:
        try:
            await _sup(request).start(name)
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (BrokerError, LiveTradingNotEnabled) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "status": _status_dict(_sup(request).status(name)[0])}

    @app.post("/api/strategies/{name}/stop")
    async def stop_strategy(name: str, request: Request) -> dict[str, Any]:
        try:
            await _sup(request).stop(name)
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "status": _status_dict(_sup(request).status(name)[0])}

    @app.post("/api/strategies/{name}/mode")
    async def set_strategy_mode(
        name: str, body: _ModeBody, request: Request
    ) -> dict[str, Any]:
        if body.mode not in _CONTROL_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown mode {body.mode!r}; expected one of {_CONTROL_MODES}",
            )
        try:
            await _sup(request).set_mode(
                name,
                body.mode,  # type: ignore[arg-type]
                confirm_live=body.confirm,
            )
        except LiveTradingNotEnabled as exc:
            # Real money without the deliberate confirmation — refuse, change nothing.
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (BrokerError, LiveTradingNotEnabled) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "status": _status_dict(_sup(request).status(name)[0])}

    if auth_token:
        _install_control_auth(app, auth_token=auth_token, templates=templates)

    return app


def _install_control_auth(
    app: FastAPI, *, auth_token: str, templates: Jinja2Templates | None
) -> None:
    """Gate ``app`` behind a token login — for remote exposure (dccd-style).

    Adds an in-memory session store, an auth-guard middleware (``401`` for
    ``/api/*``, redirect to ``/login`` for pages — except the open prefixes), a
    rate-limited ``/login`` (token → HttpOnly session cookie) and ``/logout``.
    ``/api/*`` also accepts a ``Bearer <token>`` header or ``?token=`` query for
    non-browser clients. Constant-time token comparison; ``Secure`` cookie behind
    HTTPS. Sessions are in-process (reset on restart — fine for a single daemon).
    """
    import secrets
    import time

    from fastapi.responses import RedirectResponse

    app.state.sessions = {}  # sid -> created_ns
    app.state.login_buckets = {}  # client -> (tokens, last monotonic)

    def _prune() -> None:
        cutoff = time.time_ns() - _SESSION_TTL_SECONDS * 1_000_000_000
        for sid in [s for s, ts in app.state.sessions.items() if ts < cutoff]:
            app.state.sessions.pop(sid, None)

    def _new_session() -> str:
        sid = secrets.token_urlsafe(32)
        app.state.sessions[sid] = time.time_ns()
        return sid

    def _valid_session(request: Request) -> bool:
        sid = request.cookies.get(_SESSION_COOKIE)
        if not sid:
            return False
        _prune()
        return sid in app.state.sessions

    def _is_https(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        fwd = request.headers.get("x-forwarded-proto", "")
        return fwd.split(",", 1)[0].strip() == "https"

    def _safe_next(nxt: str | None) -> str:
        if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
            return nxt
        return "/"

    def _rate_allow(key: str) -> bool:
        buckets = app.state.login_buckets
        now = time.monotonic()
        rate = _LOGIN_RATE_PER_MIN / 60.0
        tokens, last = buckets.get(key, (float(_LOGIN_RATE_PER_MIN), now))
        tokens = min(float(_LOGIN_RATE_PER_MIN), tokens + (now - last) * rate)
        if tokens < 1.0:
            buckets[key] = (tokens, now)
            return False
        buckets[key] = (tokens - 1.0, now)
        return True

    @app.middleware("http")
    async def _auth_guard(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if request.method == "OPTIONS" or path.startswith(_OPEN_PREFIXES):
            return await call_next(request)
        if path.startswith("/api/"):
            bearer = request.headers.get("Authorization") == f"Bearer {auth_token}"
            query = request.query_params.get("token") == auth_token
            if not (bearer or query or _valid_session(request)):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        elif not _valid_session(request):
            return RedirectResponse(f"/login?next={path}", status_code=303)
        return await call_next(request)

    def _login_page(request: Request, *, error: str = "", status: int = 200) -> Any:
        nxt = _safe_next(request.query_params.get("next"))
        if templates is not None:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"version": trading_bot.__version__, "next": nxt, "error": error},
                status_code=status,
            )
        return HTMLResponse(
            '<form method="post" action="/login">'
            f'<input type="hidden" name="next" value="{nxt}">'
            '<input name="token" type="password" placeholder="token">'
            "<button>Sign in</button></form>",
            status_code=status,
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Any:
        return _login_page(request)

    @app.post("/login")
    async def login_submit(request: Request) -> Any:
        client = request.client
        if not _rate_allow(client.host if client else "unknown"):
            return JSONResponse(
                {"detail": "too many attempts"},
                status_code=429,
                headers={"Retry-After": "5"},
            )
        # Parse the urlencoded login form directly (no python-multipart dep).
        from urllib.parse import parse_qs

        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        token = (form.get("token") or [""])[0]
        nxt = _safe_next((form.get("next") or ["/"])[0])
        if not secrets.compare_digest(token, auth_token):
            return _login_page(request, error="Invalid token.", status=401)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(
            _SESSION_COOKIE,
            _new_session(),
            httponly=True,
            samesite="lax",
            secure=_is_https(request),
            max_age=_SESSION_TTL_SECONDS,
        )
        return resp

    @app.post("/logout")
    async def logout(request: Request) -> Any:
        sid = request.cookies.get(_SESSION_COOKIE)
        if sid:
            app.state.sessions.pop(sid, None)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(_SESSION_COOKIE)
        return resp
