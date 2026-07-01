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
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import trading_bot
from trading_bot.application.events import (
    Event,
    FillEvent,
    LogEvent,
    OrderEvent,
)
from trading_bot.interfaces.ui import STATIC_DIR, TEMPLATES_DIR

if TYPE_CHECKING:
    from trading_bot.application.config import (
        PortfolioStrategyConfig,
        StrategyConfig,
    )
    from trading_bot.application.service_factory import Engine
    from trading_bot.application.supervisor import (
        KpiRow,
        OrderRow,
        PositionRow,
        StrategyStatus,
        StrategySupervisor,
    )
    from trading_bot.domain.fill import Fill
    from trading_bot.domain.order import Order
    from trading_bot.domain.position import Position

__all__ = ["create_app", "create_control_app", "create_dashboard_app"]


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


def _event_key(event: Event) -> str | None:
    """A dedup key for a bus event, or ``None`` when it carries no stable id.

    The merged dashboard SSE (:func:`create_dashboard_app`'s ``/api/events``)
    subscribes to several engines' buses; the same execution can, in principle,
    surface on two of them, so events are de-duplicated by this key. A
    :class:`~trading_bot.application.events.FillEvent` keys on its immutable
    ``fill_id``, an :class:`~trading_bot.application.events.OrderEvent` on the
    order's ``client_order_id`` + ``status`` (a lifecycle step is unique per
    status). A :class:`~trading_bot.application.events.LogEvent` has no stable id,
    so it returns ``None`` (never deduped — every log line is distinct).
    """
    if isinstance(event, FillEvent):
        return f"fill:{event.fill.fill_id}"
    if isinstance(event, OrderEvent):
        return f"order:{event.order.client_order_id}:{event.order.status.value}"
    return None


# ---------------------------------------------------------------------------
# Serialization — supervisor aggregate rows -> JSON-ready dicts
# ---------------------------------------------------------------------------

def _position_row_dict(row: PositionRow) -> dict[str, Any]:
    """Render a supervisor :class:`PositionRow` as a JSON-ready dict.

    The per-instrument exposure of one running unit, tagged with its ``strategy``
    and ``exchange`` (the dashboard's group-by keys) and its ``base`` asset (the
    group-by-crypto key). Money fields are exact :class:`~decimal.Decimal` strings.
    """
    return {
        "strategy": row.strategy,
        "exchange": row.exchange,
        "instrument": row.instrument,
        "base": row.base,
        "net_qty": _money_str(row.net_qty),
        "avg_entry_price": _money_str(row.avg_entry_price),
        "realised_pnl": _money_str(row.realised_pnl),
        "fees_paid": _money_str(row.fees_paid),
    }


def _order_row_dict(row: OrderRow) -> dict[str, Any]:
    """Render a supervisor :class:`OrderRow` — the order dict + strategy/venue tags."""
    return {
        "strategy": row.strategy,
        "exchange": row.exchange,
        **_order_dict(row.order),
    }


def _kpi_row_dict(row: KpiRow) -> dict[str, Any]:
    """Render a supervisor :class:`KpiRow` as a JSON-ready dict.

    Money (``realised_pnl`` / ``fees_paid``) as exact Decimal strings; the ratios
    (``sharpe`` / ``sortino`` / ``calmar`` / ``max_drawdown``) as JSON numbers at
    ``level="strategy"`` and JSON ``null`` at the aggregate levels (no combined
    curve yet).
    """
    return {
        "level": row.level,
        "key": row.key,
        "strategy": row.strategy,
        "exchange": row.exchange,
        "realised_pnl": _money_str(row.realised_pnl),
        "fees_paid": _money_str(row.fees_paid),
        "sharpe": _finite_or_none(row.sharpe),
        "sortino": _finite_or_none(row.sortino),
        "calmar": _finite_or_none(row.calmar),
        "max_drawdown": _finite_or_none(row.max_drawdown),
    }


def _finite_or_none(value: float | None) -> float | None:
    """Pass a finite float through; map ``None`` / non-finite to JSON ``null``.

    The KPI ratios can be ``inf`` / ``nan`` on a degenerate curve (a monotonic
    winner has zero drawdown → Calmar is ``inf``); a bare ``inf`` / ``nan`` is not
    valid JSON. So a non-finite (or ``None``) ratio serializes as ``null`` — the
    same "undefined estimator" convention :func:`_safe_ratio` uses for the
    single-engine view, here surfaced as an explicit ``null`` rather than ``0.0``.
    """
    if value is None or not math.isfinite(value):
        return None
    return value


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


class _CreateStrategyBody(BaseModel):
    """Request body for ``POST /api/strategies`` — deploy an existing signal.

    The UI **composes a deployment** from a signal that already exists *in code*
    (a builtin name like ``ma_crossover``, or a ``"module:function"`` ref); it
    never authors the signal's Python. This body carries only the deployment
    parameters — venue / mode / capital / universe|symbol / risk — that the
    supervisor turns into a config entry (a
    :class:`~trading_bot.application.config.StrategyConfig` for ``kind ==
    "strategy"`` or a :class:`~trading_bot.application.config.
    PortfolioStrategyConfig` for ``kind == "portfolio"``).

    Kept **module-level** (never a local class inside the factory): with ``from
    __future__ import annotations`` in force, FastAPI can only resolve a body
    model whose module globals are visible — a locally-defined model fails to
    build (the same gotcha :class:`_ModeBody` is defined at module level for).

    Attributes
    ----------
    name : str
        The deployment's logical id (unique across managed units).
    kind : {"strategy", "portfolio"}
        A single-instrument strategy or a multi-asset portfolio.
    venue : str
        The venue key the bars are read under / the deployment runs on (e.g.
        ``"binance"``, ``"kraken"``, or ``"paper"``).
    mode : {"paper", "testnet", "live"}
        The seed deployment mode. The unit is added **stopped**; going live still
        needs the typed confirmation on ``.../mode`` and the go-live gates.
    signal : str
        The signal reference: a builtin name (``"ma_crossover"``) or a
        ``"module:function"`` dotted ref to an importable callable.
    symbol : str or None
        The single pair for a ``"strategy"`` deployment (``BASE/QUOTE``).
    universe : list of str or None
        The pairs a ``"portfolio"`` deployment allocates across.
    capital : Decimal or None
        A portfolio's capital base (quote units). Required for a portfolio.
    params : dict
        Optional keyword params bound to a builtin signal (e.g. ``{"fast": 10}``).
    reference_qty : Decimal or None
        A single-instrument strategy's exposure scale (base units).
    lookback : int
        A single-instrument strategy's warmup (bars). Defaults to ``0``.
    span : int
        The bar width in seconds the deployment's dccd feed reads. Defaults to
        ``86400`` (daily — the common portfolio rebalance cadence).
    risk : dict or None
        Optional engine-wide risk limits for the deployment (merged onto the
        manifest's ``risk`` — max_position / max_order / max_daily_loss).

    """

    name: str
    kind: Literal["strategy", "portfolio"]
    venue: str
    mode: Literal["paper", "testnet", "live"] = "paper"
    signal: str
    symbol: str | None = None
    universe: list[str] | None = None
    capital: Decimal | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    reference_qty: Decimal | None = None
    lookback: int = 0
    span: int = 86_400
    risk: dict[str, Any] | None = None


def _entry_from_body(
    body: _CreateStrategyBody,
) -> StrategyConfig | PortfolioStrategyConfig:
    """Build a config entry from a create-deployment body (raises on a bad shape).

    Turns the UI's deployment parameters into the corresponding validated config
    entry — a :class:`~trading_bot.application.config.StrategyConfig` (needs a
    ``symbol``) or a :class:`~trading_bot.application.config.
    PortfolioStrategyConfig` (needs a ``universe`` + ``capital``) — pointing at
    the **already-existing** signal ``body.signal`` (a builtin name or a
    ``"module:function"`` ref). The signal code is never authored here.

    Raises
    ------
    HTTPException
        ``422`` if the deployment shape is invalid for its ``kind`` (a strategy
        without a symbol, a portfolio without a universe / capital), or the
        underlying config validation rejects the entry (an unparseable pair,
        a non-positive capital, ...).
    """
    from pydantic import ValidationError

    from trading_bot.application.config import (
        DataSourceConfig,
        PortfolioStrategyConfig,
        SignalRefConfig,
        StrategyConfig,
    )

    signal_ref = SignalRefConfig(ref=body.signal, params=dict(body.params))
    data = DataSourceConfig(exchange=body.venue, span=body.span)
    try:
        if body.kind == "strategy":
            if not body.symbol:
                raise HTTPException(
                    status_code=422,
                    detail="a 'strategy' deployment needs a 'symbol' (BASE/QUOTE)",
                )
            return StrategyConfig(
                name=body.name,
                symbol=body.symbol,
                data=data,
                signal=signal_ref,
                reference_qty=body.reference_qty,
                lookback=body.lookback,
            )
        # portfolio
        if not body.universe:
            raise HTTPException(
                status_code=422,
                detail="a 'portfolio' deployment needs a non-empty 'universe'",
            )
        if body.capital is None:
            raise HTTPException(
                status_code=422,
                detail="a 'portfolio' deployment needs a 'capital' base",
            )
        return PortfolioStrategyConfig(
            name=body.name,
            venue=body.venue,
            universe=body.universe,
            signal=signal_ref,
            capital=body.capital,
            data=data,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _discover_signals() -> dict[str, list[str]]:
    """List the deployable signal refs — builtins + a scan of ``strategies/``.

    Best-effort discovery for the UI's deploy form:

    * ``builtins`` — the single-instrument builtin names from
      :data:`~trading_bot.application.run_app._BUILTIN_SIGNALS` (e.g.
      ``ma_crossover``).
    * ``discovered`` — every ``strategies/*/signal.py`` module scanned for
      module-level callables whose name ends with ``_signal`` (the
      portfolio-signal wrapper shape, e.g. ``alloc1_portfolio_signal`` /
      ``ls1_kraken_signal``), returned as ``"module:function"`` refs (e.g.
      ``"strategies.alloc1.signal:alloc1_portfolio_signal"``).

    The scan is *tolerant*: a module that fails to import (a missing research
    dependency, a syntax error) is skipped, so a broken local strategy never
    breaks the endpoint. The engine ships no ``strategies/`` code of its own
    (it is local-only), so ``discovered`` is empty when none are present.
    """
    import importlib
    import pathlib

    from trading_bot.application.run_app import _BUILTIN_SIGNALS

    builtins = sorted(_BUILTIN_SIGNALS)

    discovered: list[str] = []
    # `strategies/` sits at the repo root, a sibling of the installed package.
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    strategies_dir = repo_root / "strategies"
    if not strategies_dir.is_dir():
        return {"builtins": builtins, "discovered": discovered}

    for signal_file in sorted(strategies_dir.glob("*/signal.py")):
        pkg = signal_file.parent.name
        module_name = f"strategies.{pkg}.signal"
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - a broken local strategy must not 500
            continue
        for attr in sorted(vars(module)):
            # A deployable signal is a public, module-level callable whose name
            # ends with `_signal` and that is *defined in this module* (so a
            # re-exported helper like `as_portfolio_signal` — imported from the
            # application layer — is skipped by the __module__ check).
            if attr.startswith("_") or not attr.endswith("_signal"):
                continue
            obj = getattr(module, attr)
            if not callable(obj):
                continue
            if getattr(obj, "__module__", None) != module_name:
                continue
            discovered.append(f"{module_name}:{attr}")

    return {"builtins": builtins, "discovered": discovered}


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


def _register_strategy_control(app: FastAPI, *, read_only: bool = False) -> None:
    """Register the shared strategy control endpoints on ``app``.

    Adds the per-strategy control surface — ``GET /api/strategies`` (each managed
    unit's status, tagged with its ``exchange``), and the write routes ``POST
    /api/strategies/{name}/start|stop|mode`` — reading the supervisor off
    ``app.state.supervisor``. **One implementation, two apps**: both
    :func:`create_control_app` and :func:`create_dashboard_app` register these, so
    the safety gates live in a single place.

    **Real money is gated** (the invariant): ``.../mode`` to ``"live"`` requires
    ``confirm: true`` in the body (:class:`_ModeBody`); without it the supervisor
    raises :class:`~trading_bot.domain.errors.LiveTradingNotEnabled` and the
    endpoint returns **403**, changing nothing.

    Parameters
    ----------
    app : FastAPI
        The app to register the routes on (must carry ``app.state.supervisor``).
    read_only : bool, optional
        When ``True``, the three **write** routes (start / stop / mode) return
        **403** and never touch the supervisor — the read-only dashboard stance.
        ``GET /api/strategies`` stays available (it is a read). Defaults to
        ``False``.

    """
    from trading_bot.domain.errors import (
        BrokerError,
        ConfigError,
        LiveTradingNotEnabled,
    )

    def _sup(request: Request) -> StrategySupervisor:
        return request.app.state.supervisor  # type: ignore[no-any-return]

    def _guard_write() -> None:
        """Refuse a write when the app is read-only (403; nothing changes)."""
        if read_only:
            raise HTTPException(
                status_code=403,
                detail="dashboard is read-only; strategy control is disabled",
            )

    @app.get("/api/strategies")
    async def strategies(request: Request) -> list[dict[str, Any]]:
        """List every managed strategy with its exchange / mode / running / PnL."""
        return [_status_dict(s) for s in _sup(request).status()]

    @app.post("/api/strategies/{name}/start")
    async def start_strategy(name: str, request: Request) -> dict[str, Any]:
        _guard_write()
        try:
            await _sup(request).start(name)
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (BrokerError, LiveTradingNotEnabled) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "status": _status_dict(_sup(request).status(name)[0])}

    @app.post("/api/strategies/{name}/stop")
    async def stop_strategy(name: str, request: Request) -> dict[str, Any]:
        _guard_write()
        try:
            await _sup(request).stop(name)
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "status": _status_dict(_sup(request).status(name)[0])}

    @app.post("/api/strategies/{name}/mode")
    async def set_strategy_mode(
        name: str, body: _ModeBody, request: Request
    ) -> dict[str, Any]:
        _guard_write()
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

    # The strategy control surface (list + start/stop/mode) is shared with the
    # unified dashboard — one implementation, one set of safety gates.
    _register_strategy_control(app)

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


# ---------------------------------------------------------------------------
# The unified dashboard — one shell hosting monitoring + control over a supervisor
# ---------------------------------------------------------------------------


#: The dashboard's page tabs: (route, template) for every server-rendered shell.
#: ``GET /`` is Overview; the rest are their own page shells. Each ``{% extends
#: "base.html" %}`` so they share the nav / health chip / connection dot.
_DASHBOARD_PAGES: tuple[tuple[str, str], ...] = (
    ("/", "overview.html"),
    ("/strategies", "strategies.html"),
    ("/orders", "orders.html"),
    ("/pnl", "pnl.html"),
    ("/logs", "logs.html"),
)

#: The dimensions ``/api/positions`` and ``/api/orders`` can be grouped by. A row
#: carries a ``strategy``, ``exchange`` and (positions only) a ``base`` crypto tag;
#: these are the group-by keys the Overview's controls offer.
_GROUP_BY_KEYS: tuple[str, ...] = ("crypto", "exchange", "strategy")

#: The KPI aggregation levels ``/api/kpi`` accepts (see
#: :meth:`~trading_bot.application.supervisor.StrategySupervisor.kpi`).
_KPI_LEVELS: tuple[str, ...] = ("strategy", "exchange", "total")


def _grouped(rows: list[dict[str, Any]], group_by: str | None) -> Any:
    """Group serialized rows by a tag, or return the flat list when ungrouped.

    With ``group_by`` ``None`` (or absent) the flat list of row dicts passes
    through unchanged. Otherwise the rows are bucketed by their ``group_by`` key
    (``"crypto"`` groups on each row's ``base`` asset; ``"exchange"`` /
    ``"strategy"`` on the eponymous tag) into ``[{"group": <key>, "rows": [...]}]``,
    preserving first-seen group order so the view is deterministic.
    """
    if group_by is None:
        return rows
    field = "base" if group_by == "crypto" else group_by
    buckets: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        key = str(row.get(field, ""))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)
    return [{"group": key, "rows": buckets[key]} for key in order]


def create_dashboard_app(
    supervisor: StrategySupervisor,
    *,
    auth_token: str | None = None,
    read_only: bool = False,
    on_change: Callable[[], None] | None = None,
) -> FastAPI:
    """Build the **unified dashboard** FastAPI over a :class:`StrategySupervisor`.

    The foundation that hosts both monitoring *and* control in one app and one
    shell (``base.html``): a top nav across Overview / Strategies / Orders / PnL /
    Logs, a brand + version + health chip + connection dot. This factory ships the
    **shell + stub pages + health** — the per-page data (positions, orders, PnL,
    logs) lands in later leaves, fetched client-side over ``/api/*``.

    Every page renders a server-side *shell only* (version + ``read_only`` + auth
    flags); no supervisor data is rendered server-side, so the pages are pure HTTP
    clients of the API. ``GET /api/health`` reports liveness + a snapshot of what
    the supervisor manages (``{status, mode, strategies, read_only}``).

    **Authentication (for remote exposure).** With ``auth_token`` set, the app is
    gated behind the same token login as the control app
    (:func:`_install_control_auth`): ``/login`` exchanges the token for an HttpOnly
    session cookie, an auth-guard middleware then refuses unauthenticated requests
    (``401`` for ``/api/*``; redirect to ``/login`` for pages), and login attempts
    are rate-limited. With ``auth_token`` ``None`` (default) there is **no** auth —
    only safe behind loopback / an SSH tunnel.

    Parameters
    ----------
    supervisor : StrategySupervisor
        The supervisor to expose, read through ``app.state.supervisor``.
    auth_token : str or None, optional
        When set, require this token to log in (enables the auth guard). ``None``
        (default) leaves the app unauthenticated — loopback / tunnel only.
    read_only : bool, optional
        When ``True``, the shell advertises a read-only stance (surfaced to the
        templates + ``app.state.read_only`` + the health payload) so later leaves
        hide/disable control affordances, **and** every mutation (create / delete
        / start / stop / mode) returns ``403``. Defaults to ``False``.
    on_change : Callable[[], None] or None, optional
        A persistence hook the mutation endpoints call **after** a successful
        membership change (create / delete), so the dashboard can rewrite its
        manifest to disk (the control plane owns the manifest). ``None`` (default)
        skips persistence — the in-memory supervisor still mutates, nothing is
        written. Typically ``lambda: supervisor.manifest().to_yaml(path)``.

    Returns
    -------
    FastAPI
        The configured dashboard application (serve via uvicorn, or a
        ``TestClient``).

    """
    app = FastAPI(
        title="trading_bot dashboard",
        summary="Unified monitoring + control dashboard over the supervisor.",
        default_response_class=_DecimalJSONResponse,
    )
    app.state.supervisor = supervisor
    app.state.read_only = read_only
    app.state.auth_enabled = bool(auth_token)
    app.state.on_change = on_change

    def _sup(request: Request) -> StrategySupervisor:
        """Read the wired supervisor off ``app.state`` (explicit, testable access)."""
        return request.app.state.supervisor  # type: ignore[no-any-return]

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = (
        Jinja2Templates(directory=str(TEMPLATES_DIR))
        if TEMPLATES_DIR.is_dir()
        else None
    )

    if templates is not None:

        def _page(route: str, template: str) -> None:
            """Register one page route rendering ``template`` (the base shell).

            Bound in a helper so each of the five pages closes over its own
            ``route`` / ``template`` (a bare loop would late-bind them all to the
            last iteration). Every page passes ``{active, version, read_only,
            auth}`` so the shared nav highlights the active tab and the footer /
            JS see the read-only + auth flags.
            """

            @app.get(route, response_class=HTMLResponse, name=f"page:{route}")
            async def page(request: Request) -> Any:
                return templates.TemplateResponse(
                    request,
                    template,
                    {
                        "active": route,
                        "version": trading_bot.__version__,
                        "read_only": request.app.state.read_only,
                        "auth": request.app.state.auth_enabled,
                    },
                )

        for _route, _template in _DASHBOARD_PAGES:
            _page(_route, _template)

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        """Liveness + a snapshot of what the supervisor manages."""
        sup = _sup(request)
        return {
            "status": "ok",
            "mode": sup.mode,
            "strategies": len(sup.names()),
            "read_only": request.app.state.read_only,
        }

    # -- Positions (aggregated across the units, groupable) ------------------ #

    @app.get("/api/positions")
    async def positions(request: Request, group_by: str | None = None) -> Any:
        """Net positions across every running unit, tagged strategy + exchange.

        A read (safe under ``read_only``). With ``?group_by=crypto|exchange|
        strategy`` the rows are bucketed into ``[{"group", "rows"}]``; ungrouped
        otherwise. Money is rendered as exact Decimal strings.
        """
        if group_by is not None and group_by not in _GROUP_BY_KEYS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown group_by {group_by!r}; expected one of {_GROUP_BY_KEYS}",
            )
        rows = [_position_row_dict(row) for row in _sup(request).positions()]
        return _grouped(rows, group_by)

    # -- Orders (open, aggregated across the units, groupable) --------------- #

    @app.get("/api/orders")
    async def orders(request: Request, group_by: str | None = None) -> Any:
        """Open (non-terminal) orders across every running unit, same grouping."""
        if group_by is not None and group_by not in _GROUP_BY_KEYS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown group_by {group_by!r}; expected one of {_GROUP_BY_KEYS}",
            )
        rows = [_order_row_dict(row) for row in _sup(request).open_orders()]
        # Orders carry no crypto tag; group-by-crypto folds them by base asset via
        # the order dict's instrument. Add the base so `_grouped` can key on it.
        for row in rows:
            row["base"] = str(row.get("instrument", "")).split("/", 1)[0]
        return _grouped(rows, group_by)

    # -- KPI (three levels: strategy / exchange / total) --------------------- #

    @app.get("/api/kpi")
    async def kpi(request: Request, level: str = "strategy") -> list[dict[str, Any]]:
        """Realised PnL + fees (+ per-strategy ratios) at ``level``.

        ``?level=strategy|exchange|total`` (see
        :meth:`~trading_bot.application.supervisor.StrategySupervisor.kpi`): money
        as exact Decimal strings, per-strategy ratios as JSON numbers (``null`` at
        the aggregate levels).
        """
        if level not in _KPI_LEVELS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown level {level!r}; expected one of {_KPI_LEVELS}",
            )
        return [
            _kpi_row_dict(row)
            for row in _sup(request).kpi(level)  # type: ignore[arg-type]
        ]

    # -- SSE events (merged across every unit's engine bus) ------------------ #

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        """Server-Sent-Events fanning **every** running unit's bus onto one feed.

        Registers a fresh queue on each running unit's engine
        :class:`~trading_bot.application.events.EventBus` and multiplexes them onto
        a single generator, yielding each event as a ``data: <json>\\n\\n`` frame
        (money as Decimal strings, tagged with a ``type``). Order and fill events
        are de-duplicated by their domain id so an execution seen on two buses is
        emitted once. Every registered queue is unregistered in a ``finally`` on
        disconnect. Read-only — subscribing observes; it never trades.
        """
        sup = _sup(request)
        # Snapshot the running engines' buses now; the merged stream is over the
        # set live at connect time (a unit started later is picked up on reconnect,
        # like the single-engine SSE view).
        buses = [
            unit.engine.bus
            for unit in sup._running_units()  # noqa: SLF001 — read the wired buses
            if unit.engine is not None
        ]
        queues = [bus.add_queue() for bus in buses]

        async def _generator() -> Any:
            seen: set[str] = set()
            try:
                yield ": connected\n\n"
                if not queues:
                    # No running unit — keep the connection alive with heartbeats so
                    # the client's EventSource stays open until a reconnect finds one.
                    while not await request.is_disconnected():
                        await asyncio.sleep(15.0)
                        yield ": heartbeat\n\n"
                    return
                while True:
                    if await request.is_disconnected():
                        break
                    # Wait on whichever queue produces first (bounded, so the loop
                    # periodically re-checks disconnection and heartbeats).
                    getters = [asyncio.ensure_future(q.get()) for q in queues]
                    done, pending = await asyncio.wait(
                        getters,
                        timeout=15.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    if not done:
                        yield ": heartbeat\n\n"
                        continue
                    for task in done:
                        event = task.result()
                        key = _event_key(event)
                        if key is not None and key in seen:
                            continue  # same execution on two buses — emit once.
                        if key is not None:
                            seen.add(key)
                        yield (
                            f"data: {json.dumps(_event_dict(event), default=_default)}\n\n"
                        )
            finally:
                for bus, queue in zip(buses, queues, strict=True):
                    bus.remove_queue(queue)

        return StreamingResponse(_generator(), media_type="text/event-stream")

    # -- Signal discovery (deployable refs for the create form) -------------- #

    @app.get("/api/signals")
    async def signals(request: Request) -> dict[str, list[str]]:
        """Deployable signal refs — builtins + a scan of ``strategies/*/signal.py``.

        A read (safe under ``read_only``): ``{builtins: [...], discovered:
        ["strategies.alloc1.signal:alloc1_portfolio_signal", ...]}``. The UI picks
        one of these to compose a deployment — it never authors signal code.
        """
        return _discover_signals()

    # -- Deployment CRUD: add / remove a strategy, persist the manifest ------ #

    def _persist(request: Request) -> None:
        """Rewrite the manifest after a membership change (no-op without a hook)."""
        hook = request.app.state.on_change
        if hook is not None:
            hook()

    @app.post("/api/strategies")
    async def create_strategy(
        body: _CreateStrategyBody, request: Request
    ) -> dict[str, Any]:
        """Deploy an existing signal as a new **stopped** unit, then persist.

        Composes a config entry from the deployment body (venue / mode / capital /
        universe|symbol / signal ref), adds it to the supervisor (validated the
        same way ``__init__`` splits config → units — a bad signal ref / no
        matching broker for a non-paper mode is rejected, nothing added), and
        **persists the manifest** so the deployment survives a restart. The unit
        is added stopped — deploying never auto-trades (paper-safe).
        """
        from trading_bot.domain.errors import ConfigError

        if request.app.state.read_only:
            raise HTTPException(
                status_code=403,
                detail="dashboard is read-only; strategy deployment is disabled",
            )
        sup = _sup(request)
        entry = _entry_from_body(body)
        try:
            name = sup.add_unit(entry)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _persist(request)
        return {"ok": True, "status": _status_dict(sup.status(name)[0])}

    @app.delete("/api/strategies/{name}")
    async def delete_strategy(name: str, request: Request) -> dict[str, Any]:
        """Stop (if running) and remove a managed unit, then persist the manifest."""
        from trading_bot.domain.errors import ConfigError

        if request.app.state.read_only:
            raise HTTPException(
                status_code=403,
                detail="dashboard is read-only; strategy removal is disabled",
            )
        sup = _sup(request)
        try:
            sup.remove_unit(name)
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _persist(request)
        return {"ok": True, "removed": name}

    # The strategy control surface (list + start/stop/mode) — shared with the
    # control app. Under `read_only`, the write routes return 403 (the reads stay).
    _register_strategy_control(app, read_only=read_only)

    if auth_token:
        _install_control_auth(app, auth_token=auth_token, templates=templates)

    return app
