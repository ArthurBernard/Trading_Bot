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
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from trading_bot.application.events import (
    Event,
    FillEvent,
    LogEvent,
    OrderEvent,
)

if TYPE_CHECKING:
    from trading_bot.application.service_factory import Engine
    from trading_bot.domain.fill import Fill
    from trading_bot.domain.order import Order
    from trading_bot.domain.position import Position

__all__ = ["create_app"]


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

    The fynance-backed ratio estimators reject some real equity curves the
    fill-driven :class:`~trading_bot.application.performance_service.
    PerformanceService` produces: with the factory's default ``v0 = 0`` the
    equity series starts at (or crosses) zero the moment a fee is charged, and
    fynance raises a :class:`ValueError` ("initial value cannot be null" / "must
    be of the same sign"). A read-only KPI view must not turn that into a 500;
    instead it reports ``0.0`` — the same "undefined estimator → 0.0" convention
    the service already uses for a too-short series.
    """
    try:
        return compute()
    except (ValueError, ZeroDivisionError, ArithmeticError):
        return 0.0


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
