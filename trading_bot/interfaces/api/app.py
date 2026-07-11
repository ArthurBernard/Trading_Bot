"""FastAPI application — the unified dashboard over the live engine(s).

The entrypoints are :func:`create_dashboard_app` (the unified monitoring +
control dashboard over a :class:`~trading_bot.application.supervisor.StrategySupervisor`
— every managed strategy's positions, tracked orders and PnL/KPI as JSON, plus a
Server-Sent-Events stream of order/fill/log events) and :func:`create_control_app`
(a thin backward-compat alias of the same app). This module also holds the shared
serialization helpers (money-as-Decimal-string, SSE framing) and HTTP hardening
both factories build on.

No order-placement route — a hard invariant (carried into the ADR)
--------------------------------------------------------------------
**No endpoint places, amends or cancels an order.** The write path
(:class:`~trading_bot.application.order_router.OrderRouter`) is reachable only
in-process by the strategy runner, never from the network — a web client can
*observe* the engine and, through the gated control routes, start/stop a unit or
switch its mode, but it can never place a trade directly. This keeps the only
money-moving surface (order submission) off the HTTP boundary entirely, so a
compromised or misused web client cannot place an order. Going ``live`` is
further gated by a typed acknowledgement enforced server-side (see
:func:`create_dashboard_app`), and ``read_only=True`` refuses every mutation
outright (``403``).

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
:func:`create_dashboard_app` mounts the unified web dashboard: ``StaticFiles`` at
``/static`` over :data:`~trading_bot.interfaces.ui.STATIC_DIR`, a
:class:`~fastapi.templating.Jinja2Templates` over
:data:`~trading_bot.interfaces.ui.TEMPLATES_DIR`, and one page per tab (Overview /
Strategies / Orders / Logs), the per-strategy detail page (``/strategies/{name}``)
and the deploy form (``/strategies/new``), rendered as **shells** carrying only the version
and ``read_only``/auth flags (no supervisor data server-side). Each page's script
fetches ``/api/*`` and live-updates from ``/api/events``, so the UI is a **pure
HTTP client** of this API. The directories are resolved from the installed package
(shipped via ``[tool.setuptools.package-data]``), and the mount is guarded on their
existence so the API still builds if assets are absent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import secrets
import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import trading_bot
from trading_bot.application.display_ccy import convert, resolve_currency
from trading_bot.application.events import (
    Event,
    FillEvent,
    LogEvent,
    OrderEvent,
)
from trading_bot.interfaces.ui import STATIC_DIR, TEMPLATES_DIR

if TYPE_CHECKING:
    from trading_bot.application.config import (
        AppConfig,
        PortfolioStrategyConfig,
        StrategyConfig,
    )
    from trading_bot.application.supervisor import (
        BalanceRow,
        FillRow,
        KpiRow,
        OrderRow,
        PositionRow,
        StrategyStatus,
        StrategySupervisor,
    )
    from trading_bot.domain.fill import Fill
    from trading_bot.domain.order import Order
    from trading_bot.domain.position import Position

__all__ = ["create_control_app", "create_dashboard_app"]

logger = logging.getLogger(__name__)


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


def _epoch_ms() -> int:
    """The current server time, as integer epoch milliseconds.

    Stamps every SSE frame with a server-side ``ts`` so the Logs page shows *when
    the server emitted the event*, not the client's receive time (which drifts
    under latency / reconnects).
    """
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Serialization — supervisor aggregate rows -> JSON-ready dicts
# ---------------------------------------------------------------------------


def _position_row_dict(row: PositionRow, config: AppConfig) -> dict[str, Any]:
    """Render a supervisor :class:`PositionRow` as a JSON-ready dict.

    The per-instrument exposure of one running unit, tagged with its ``strategy``
    and ``exchange`` (the dashboard's group-by keys) and its ``base`` asset (the
    group-by-crypto key). Money fields are exact :class:`~decimal.Decimal` strings.
    ``mark_asof_ts`` is already an integer epoch ms (or ``None``), like
    ``last_asof_ts`` on ``/api/strategies`` — no stringification needed.

    ``display_currency`` (:func:`~trading_bot.application.display_ccy.
    resolve_currency`, resolved for the row's ``exchange``) + ``value_display``
    / ``unrealised_display`` (:func:`~trading_bot.application.display_ccy.convert`,
    from the row's ``fee_ccy`` quote) are additive — ``None`` (JSON ``null``)
    when no rate is declared for that quote, never a guessed conversion.
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
        "mark": _money_str(row.mark),
        "mark_asof_ts": row.mark_asof_ts,
        "mark_source": row.mark_source,
        "value": _money_str(row.value),
        "unrealised": _money_str(row.unrealised),
        "fee_ccy": row.fee_ccy,
        "display_currency": resolve_currency(row.exchange, config),
        "value_display": _money_str(convert(row.value, row.fee_ccy, config)),
        "unrealised_display": _money_str(convert(row.unrealised, row.fee_ccy, config)),
    }


def _order_row_dict(row: OrderRow) -> dict[str, Any]:
    """Render a supervisor :class:`OrderRow` — the order dict + strategy/venue tags.

    ``ts`` (epoch ms, or ``null``) is when the order was **first** persisted to
    the unit's store — already an integer, unlike the money fields, so it needs
    no stringification.
    """
    return {
        "strategy": row.strategy,
        "exchange": row.exchange,
        # The instrument's base asset — the crypto filter key (orders carry no base
        # tag of their own; derive it from the instrument for the ?crypto= filter).
        "base": str(row.order.instrument).split("/", 1)[0],
        "ts": row.ts,
        **_order_dict(row.order),
    }


def _fill_row_dict(row: FillRow) -> dict[str, Any]:
    """Render a supervisor :class:`FillRow` — the fill dict + strategy/venue/base tags."""
    return {
        "strategy": row.strategy,
        "exchange": row.exchange,
        "base": row.base,
        **_fill_dict(row.fill),
    }


def _balance_row_dict(row: BalanceRow) -> dict[str, Any]:
    """Render a supervisor :class:`BalanceRow` as a JSON-ready dict.

    ``balances`` is a per-asset mapping to exact Decimal strings (never a plain
    ``str(dict)`` — each amount is stringified individually so no value ever
    round-trips through ``float``). ``error`` is ``None`` (JSON ``null``) on a
    successful fetch, or the broker's error message when its ``balances()`` call
    raised — the row still renders (an empty ``balances`` dict), never a 500.
    """
    return {
        "strategy": row.strategy,
        "exchange": row.exchange,
        "mode": row.mode,
        "balances": {
            asset: _money_str(amount) for asset, amount in row.balances.items()
        },
        "error": row.error,
    }


def _kpi_row_dict(row: KpiRow, config: AppConfig) -> dict[str, Any]:
    """Render a supervisor :class:`KpiRow` as a JSON-ready dict.

    Money (``realised_pnl`` / ``fees_paid``) as exact Decimal strings; the ratios
    (``sharpe`` / ``sortino`` / ``calmar`` / ``max_drawdown``) as JSON numbers at
    ``level="strategy"`` and JSON ``null`` at the aggregate levels (no combined
    curve yet); ``quote`` is the row's quote currency, or ``null`` when the row
    folds units that mix quote currencies (the UI renders "mixed").

    ``display_currency`` (resolved for the row's ``exchange``, ``None`` at
    ``level="total"``) + ``realised_pnl_display`` / ``fees_paid_display``
    (converted from ``quote`` — the row's only money aggregates, mirroring
    :func:`_position_row_dict`'s pair) are additive — ``None`` (JSON ``null``)
    when no rate is declared, or ``quote`` itself is ``None`` (mixed), never a
    guessed conversion.
    """
    return {
        "level": row.level,
        "key": row.key,
        "strategy": row.strategy,
        "exchange": row.exchange,
        "quote": row.quote,
        "realised_pnl": _money_str(row.realised_pnl),
        "fees_paid": _money_str(row.fees_paid),
        "sharpe": _finite_or_none(row.sharpe),
        "sortino": _finite_or_none(row.sortino),
        "calmar": _finite_or_none(row.calmar),
        "max_drawdown": _finite_or_none(row.max_drawdown),
        "display_currency": resolve_currency(row.exchange, config),
        "realised_pnl_display": _money_str(
            convert(row.realised_pnl, row.quote, config)
        ),
        "fees_paid_display": _money_str(convert(row.fees_paid, row.quote, config)),
    }


def _pnl_series_dict(
    result: dict[str, Any], *, only_mode: str | None = None
) -> dict[str, Any]:
    """Render a supervisor :meth:`pnl_series` result as a JSON-ready dict.

    The per-mode realised-PnL / equity curve: ``v0`` and every money field in the
    series points (``[ts_ms, pnl, equity]``) and the ``current`` end points
    (``equity`` / ``unrealised`` / ``allocation`` / ``contributed`` /
    ``total_value``) are exact :class:`~decimal.Decimal` strings, with
    ``capital_policy`` passed through as a plain string; ``ts_ms`` stays the
    integer ms it already is. With ``only_mode`` set, only that mode's series +
    current are kept (the ``?mode=`` filter) — an absent mode yields empty
    ``series`` / ``current`` (200, not an error).
    """
    series_in: dict[str, Any] = result["series"]
    current_in: dict[str, Any] = result["current"]
    modes = (
        list(series_in)
        if only_mode is None
        else [m for m in series_in if m == only_mode]
    )
    series_out = {
        mode: [[pt[0], _money_str(pt[1]), _money_str(pt[2])] for pt in series_in[mode]]
        for mode in modes
    }
    current_out = {
        mode: {
            "equity": _money_str(current_in[mode]["equity"]),
            "unrealised": _money_str(current_in[mode]["unrealised"]),
            "allocation": _money_str(current_in[mode]["allocation"]),
            "contributed": _money_str(current_in[mode]["contributed"]),
            "total_value": _money_str(current_in[mode]["total_value"]),
            "capital_policy": current_in[mode]["capital_policy"],
        }
        for mode in modes
        if mode in current_in
    }
    return {
        "strategy": result["strategy"],
        "v0": _money_str(result["v0"]),
        "series": series_out,
        "current": current_out,
    }


def _capital_breakdown_dict(breakdown: dict[str, Any]) -> dict[str, Any]:
    """Render a supervisor :meth:`capital_breakdown` result for JSON (money as strings).

    Every money field (``allocation`` / ``contributed`` / ``realised`` /
    ``unrealised`` / ``total_value`` / ``withdrawable`` and each ledger event's
    ``amount``) is an exact :class:`~decimal.Decimal` string (``None`` passes
    through); ``policy`` is a plain string and each event ``ts`` the integer ms it
    already is. ``events`` is the ledger audit trail (deposits / withdrawals /
    the genesis funding) the UI renders.
    """
    return {
        "strategy": breakdown["strategy"],
        "allocation": _money_str(breakdown["allocation"]),
        "contributed": _money_str(breakdown["contributed"]),
        "realised": _money_str(breakdown["realised"]),
        "unrealised": _money_str(breakdown["unrealised"]),
        "total_value": _money_str(breakdown["total_value"]),
        "withdrawable": _money_str(breakdown["withdrawable"]),
        "policy": breakdown["policy"],
        "events": [
            {
                "event_id": event["event_id"],
                "type": event["type"],
                "amount": _money_str(event["amount"]),
                "ts": event["ts"],
                "note": event["note"],
            }
            for event in breakdown["events"]
        ],
    }


def _finite_or_none(value: float | None) -> float | None:
    """Pass a finite float through; map ``None`` / non-finite to JSON ``null``.

    The KPI ratios can be ``inf`` / ``nan`` on a degenerate curve (a monotonic
    winner has zero drawdown → Calmar is ``inf``); a bare ``inf`` / ``nan`` is not
    valid JSON. So a non-finite (or ``None``) ratio serializes as ``null`` — the
    same "undefined estimator" convention a raised/non-finite ratio maps to
    elsewhere, here surfaced as an explicit ``null``.
    """
    if value is None or not math.isfinite(value):
        return None
    return value


# ---------------------------------------------------------------------------
# Shared HTTP hardening — body-size cap + security headers (I-6, I-11)
# ---------------------------------------------------------------------------

#: The largest request body (bytes) the app accepts before returning ``413``. A
#: control-plane body is a small JSON deploy/mode descriptor — 64 KiB is generous —
#: so an oversized ``Content-Length`` (or a body that streams past the cap) is a
#: memory/disk-amplification DoS vector (I-6), not a legitimate request. The
#: read-only view has no bodies at all; the cap is a cheap safety net either way.
_MAX_BODY_BYTES = 64 * 1024

#: The security-response headers set on **every** response (I-11). ``nosniff`` stops
#: content-type sniffing; ``DENY`` / ``frame-ancestors 'none'`` block clickjacking of
#: the control UI; ``no-referrer`` keeps paths/tokens out of the ``Referer``. These
#: are free on a private tailnet and a second line of defence if the surface grows.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


class _SuppressGracefulShutdownCancellation(logging.Filter):
    """Drop uvicorn's "Exception in ASGI application" record for an expected
    graceful-shutdown cancellation — not a real application bug.

    Why this lives at the logging layer, not in a route
    -----------------------------------------------------
    Every ``/api/events`` route below returns a plain
    :class:`~starlette.responses.StreamingResponse`. uvicorn 0.49 declares ASGI
    ``spec_version: "2.3"`` for HTTP (see
    ``uvicorn/protocols/http/httptools_impl.py``), which is *below* the
    ``(2, 4)`` threshold Starlette's own ``StreamingResponse.__call__`` checks
    before picking its newer, single-await implementation — so under this
    uvicorn version Starlette *always* falls back to its older code path: an
    ``anyio`` task group racing "stream the body" against "listen for
    disconnect" (``starlette/responses.py``). When
    ``timeout_graceful_shutdown`` (:data:`_SHUTDOWN_GRACE_SECONDS`) elapses on a
    still-open SSE connection, uvicorn force-cancels the per-connection task —
    logging "Cancel N running task(s), timeout graceful shutdown exceeded" (a
    legitimate, expected line, left alone). That cancellation lands *inside
    Starlette's own task group*, which re-raises it unconverted all the way
    back up to uvicorn's ``run_asgi``, which logs **any** exception escaping the
    ASGI app — even a deliberate cancellation it just issued itself — as an
    ERROR-level, multi-frame traceback ("Exception in ASGI application").
    Operators read that as a crash on every shutdown while a client holds
    ``/api/events`` open, even though the SSE generators in this module (see
    their own ``except asyncio.CancelledError`` handling) already end cleanly
    the moment cancellation reaches *their* frame — the noisy traceback
    originates in Starlette's own plumbing, which this module does not control,
    so suppressing it has to happen at the logging layer instead. This filter
    drops *only* that exact, expected shape — an :class:`asyncio.CancelledError`
    reaching uvicorn's "Exception in ASGI application" log call — so a genuine
    application exception (any other exception type, or a ``CancelledError``
    logged under a different message) is still logged normally.

    Matching on exception *type* alone (not also uvicorn's own cancel message,
    e.g. "Task cancelled, timeout graceful shutdown exceeded") is deliberate:
    with **two** stacked ``@app.middleware("http")`` handlers here (see
    :func:`_install_hardening`), each ``BaseHTTPMiddleware`` layer's own nested
    ``anyio`` task group can re-signal the cancellation through its *own*
    cancel scope on the way back out, which — per ``anyio``'s cancel-scope
    bookkeeping — can substitute a **fresh, message-less**
    ``CancelledError()`` for the one uvicorn originally raised. The message is
    the only thing lost; every step in the chain is still a plain
    cancellation. Restricting the match to uvicorn's own "Exception in ASGI
    application" message keeps this from ever catching an unrelated
    ``uvicorn.error`` record.
    """

    _EXPECTED_MESSAGE_PREFIX = "Exception in ASGI application"

    def filter(self, record: logging.LogRecord) -> bool:
        """Return ``False`` (drop) only for the expected shutdown cancellation."""
        if not record.exc_info:
            return True
        exc = record.exc_info[1]
        is_expected = isinstance(
            exc, asyncio.CancelledError
        ) and record.getMessage().startswith(self._EXPECTED_MESSAGE_PREFIX)
        return not is_expected


def _suppress_graceful_shutdown_cancellation_logs() -> None:
    """Install :class:`_SuppressGracefulShutdownCancellation` on ``uvicorn.error`` once.

    Idempotent (checked by filter *type*), so calling this from every
    :func:`create_dashboard_app` build — including in tests, which build many
    apps per process — never stacks up duplicate filter instances.
    """
    target = logging.getLogger("uvicorn.error")
    if not any(
        isinstance(f, _SuppressGracefulShutdownCancellation) for f in target.filters
    ):
        target.addFilter(_SuppressGracefulShutdownCancellation())


def _install_hardening(app: FastAPI) -> None:
    """Add the shared body-size cap + security-header middleware to ``app``.

    One place for the transport hardening both factories share:

    * **I-6 — request-body cap.** A request whose ``Content-Length`` exceeds
      :data:`_MAX_BODY_BYTES`, *or* whose actually-read body does, is refused
      ``413`` before any handler runs — so an oversized deploy/login body cannot
      amplify into a giant manifest / filename / memory spike.
    * **I-11 — security headers + no-store.** Every response carries
      :data:`_SECURITY_HEADERS` (nosniff / anti-clickjacking / no-referrer), and
      every authed JSON body (``/api/*``) additionally gets ``Cache-Control:
      no-store`` so the live book is never cached by a browser or intermediary.
    * **Quiet shutdown.** Installs
      :class:`_SuppressGracefulShutdownCancellation` on the ``uvicorn.error``
      logger so a force-cancelled ``/api/events`` connection past
      :data:`_SHUTDOWN_GRACE_SECONDS` doesn't spam an ERROR-level traceback for
      what is an expected, controlled shutdown (see that class's docstring).

    Middleware runs outermost-first in registration order; the body cap is added
    last here so it runs **first** (it can reject before the header middleware even
    builds a response).
    """
    _suppress_graceful_shutdown_cancellation_logs()

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        # Authed JSON (the live book) must never be cached by a browser or a
        # proxy — set no-store on the API surface (the HTML shell may still be
        # cached; it carries no engine data).
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def _limit_body_size(request: Request, call_next: Any) -> Any:
        # Reject early on a declared oversized Content-Length (cheap, no read).
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > _MAX_BODY_BYTES:
                    return JSONResponse(
                        {"detail": "request body too large"}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"detail": "invalid Content-Length"}, status_code=400
                )
        # Guard the chunked / missing-length case: buffer the body once, cap it, and
        # re-inject it so the downstream handler still reads it (Starlette caches the
        # body on the request after the first `.body()`).
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            body = await request.body()
            if len(body) > _MAX_BODY_BYTES:
                return JSONResponse(
                    {"detail": "request body too large"}, status_code=413
                )
        return await call_next(request)


# ---------------------------------------------------------------------------
# The control app — the daemon's read+write dashboard over a StrategySupervisor
# ---------------------------------------------------------------------------


#: Valid deployment modes the control API accepts.
_CONTROL_MODES = ("paper", "testnet", "live")

#: The exact phrase a client must type + submit to switch a unit to ``live`` (real
#: money). Enforced **server-side** (not just in the browser modal): the mode route
#: refuses a live switch unless the request body carries this ack verbatim, so a raw
#: ``{"mode":"live","confirm":true}`` — bypassing the UI — cannot flip to live.
_LIVE_ACK_PHRASE = "I UNDERSTAND"

#: Browser session cookie set on a successful /login (opaque, HttpOnly).
_SESSION_COOKIE = "tb_session"
#: How long a control session stays valid (seconds).
_SESSION_TTL_SECONDS = 12 * 3600
#: Login attempts per minute per client (brute-force throttle).
_LOGIN_RATE_PER_MIN = 10
#: Path prefixes reachable without a session (the login flow + assets).
#: ``/favicon.ico`` is open on purpose: a browser fetches it in the background on
#: every page — including the login page — and an auth redirect to ``/login``
#: would re-render the form and (before the stable-cookie fix in ``_login_page``)
#: rotate the CSRF cookie under the form the user is looking at.
_OPEN_PREFIXES = ("/login", "/logout", "/static", "/favicon.ico")

#: Shape of a CSRF token we minted (``secrets.token_urlsafe(32)`` → 43 urlsafe
#: chars). An existing cookie is only reused when it matches, so an arbitrary
#: value can never be echoed back into the login form.
_CSRF_SHAPE = re.compile(r"^[A-Za-z0-9_-]{43}$")

#: Hard cap on live sessions (I-7): once reached, the oldest session is evicted on a
#: new login so the in-memory map cannot grow without bound over a long-lived daemon.
_MAX_SESSIONS = 1024
#: A login rate-bucket idle for longer than this (seconds) is pruned (I-7): the
#: buckets are keyed by peer address and were never swept, so every distinct source
#: that ever hit ``/login`` used to leave a permanent entry. One TTL wider than the
#: refill window (a bucket idle this long has fully refilled — dropping it is a no-op).
_RATE_BUCKET_TTL_SECONDS = 3600
#: The hidden ``/login`` form field carrying the double-submit CSRF token (I-13). The
#: token is minted into a cookie on ``GET /login`` and must be echoed in the POST.
_CSRF_COOKIE = "tb_csrf"
_CSRF_FIELD = "csrf"

#: Whether the app trusts a client-supplied ``X-Forwarded-Proto`` to decide the
#: ``Secure`` cookie flag (I-4). **Off** for the direct-uvicorn tailnet deployment:
#: the header is fully client-controlled there (no stripping proxy), and the path is
#: plain-HTTP over WireGuard — so forcing ``Secure`` on would only self-break the
#: cookie. Only a real ``https`` request scheme sets ``Secure`` unless a trusted
#: proxy is explicitly introduced (then flip this and strip/overwrite the header at
#: the proxy). The tailnet's own encryption — not this flag — protects the cookie.
_TRUST_FORWARDED_PROTO = False

#: The module prefixes a deploy-body ``signal.ref`` may import from (I-1). A dotted
#: ``"module:function"`` ref is handed to :func:`importlib.import_module` at unit
#: start — arbitrary-module import is RCE-adjacent for a token holder. Confining the
#: importable module to these prefixes shrinks the blast radius to the trading
#: stack's own signal code (local ``strategies/`` + the research/execution repos).
#: A ref must start with one of these (as a dotted-path segment boundary) or the
#: deploy is rejected 400.
_SIGNAL_REF_ALLOWED_PREFIXES = (
    "strategies",
    "fynance",
    "fynance_research",
    "trading_bot",
)

#: A conservative shape check for the module part of a ``"module:function"`` ref:
#: dotted identifiers only (each segment a Python identifier). Rejects paths with
#: separators, spaces or other injection-ish characters before any import happens.
_SIGNAL_REF_MODULE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"
)


class _ModeBody(BaseModel):
    """Request body for ``POST /api/strategies/{name}/mode``.

    Switching to ``live`` (real money) is gated **server-side** by a typed
    acknowledgement, not merely a boolean: the client must submit ``ack`` equal to
    :data:`_LIVE_ACK_PHRASE` (verbatim, case-sensitive) — the same phrase the
    browser modal makes the operator type. ``confirm`` is kept as a coarse flag for
    backward compatibility, but a live switch requires the exact ``ack`` phrase; a
    bare ``{"mode":"live","confirm":true}`` (bypassing the UI) is refused with 403.
    ``paper`` / ``testnet`` need neither.
    """

    mode: str
    confirm: bool = False
    ack: str | None = None


class _CapitalOpBody(BaseModel):
    """Request body for ``POST /api/strategies/{name}/capital`` — deposit / withdraw.

    ``amount`` is a **string** (never a JSON float): money crosses the wire as an
    exact decimal string, so a client that sends ``12.5`` as a float is rejected
    (422) rather than silently binary-rounded — the money-exactness invariant.
    ``op_id`` is the caller-assigned idempotency key (the client-order-id
    analogue): re-POSTing the same ``op_id`` is a no-op that returns the unchanged
    breakdown. Kept **module-level** for the same reason as :class:`_ModeBody`
    (FastAPI resolves body models against module globals under ``from __future__
    import annotations``).
    """

    action: Literal["deposit", "withdraw"]
    # I-6: bound the free-form strings so a giant field cannot amplify (the
    # body-size middleware is the coarse gate; these are the per-field defence).
    amount: str = Field(min_length=1, max_length=64)
    op_id: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=256)


class _PolicyBody(BaseModel):
    """Request body for ``POST /api/strategies/{name}/policy`` — set the sizing policy.

    Module-level (see :class:`_CapitalOpBody`). ``policy`` is the capital-evolution
    policy the unit sizes under: ``fixed`` sizes against contributed capital,
    ``compound`` reinvests realised PnL into the base.
    """

    policy: Literal["fixed", "compound"]


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
        The seed deployment mode. **Must match the manifest's global seed mode**
        (deployments inherit it — the supervisor seeds every new unit from the
        manifest, not per-entry): a ``mode`` the server would otherwise discard is
        rejected ``422`` (I-9), so the field is meaningful rather than misleading.
        The unit is added **stopped**; going live still needs the typed confirmation
        on ``.../mode`` and the go-live gates.
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
    db_path : str or None
        An explicit per-strategy SQLite store path isolating this deployment's
        book/PnL from the global store. ``None`` (default) → the deploy endpoint
        **auto-assigns** one under ``<manifest-storage-dir>/dashboard/<name>.sqlite``
        so a UI-deployed strategy is isolated by default (no commingling with
        another deployment in the same manifest).

    """

    # I-6: bound every free-form string so a giant field cannot amplify into a giant
    # manifest / on-disk filename / memory spike (the body-size middleware is the
    # coarse gate; these are the per-field defence in depth). A name / venue / symbol
    # / signal ref is a short identifier; a universe is a handful of pairs.
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["strategy", "portfolio"]
    venue: str = Field(min_length=1, max_length=64)
    mode: Literal["paper", "testnet", "live"] = "paper"
    signal: str = Field(min_length=1, max_length=256)
    symbol: str | None = Field(default=None, max_length=64)
    universe: list[str] | None = Field(default=None, max_length=256)
    capital: Decimal | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    reference_qty: Decimal | None = None
    lookback: int = 0
    span: int = 86_400
    risk: dict[str, Any] | None = None
    db_path: str | None = Field(default=None, max_length=512)


def _entry_from_body(
    body: _CreateStrategyBody, *, db_path: str | None = None
) -> StrategyConfig | PortfolioStrategyConfig:
    """Build a config entry from a create-deployment body (raises on a bad shape).

    Turns the UI's deployment parameters into the corresponding validated config
    entry — a :class:`~trading_bot.application.config.StrategyConfig` (needs a
    ``symbol``) or a :class:`~trading_bot.application.config.
    PortfolioStrategyConfig` (needs a ``universe`` + ``capital``) — pointing at
    the **already-existing** signal ``body.signal`` (a builtin name or a
    ``"module:function"`` ref). The signal code is never authored here.

    Parameters
    ----------
    body : _CreateStrategyBody
        The deployment parameters.
    db_path : str or None, optional
        The per-strategy store path to stamp onto the entry so its book/PnL are
        isolated from the global store (the endpoint resolves ``body.db_path`` or
        an auto-assigned default before calling in). ``None`` leaves the entry on
        the global store.

    Raises
    ------
    HTTPException
        ``400`` if the ``signal`` ref is outside the import allow-list (I-1);
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

    # I-1: gate the signal ref before it can reach import_module at unit start.
    _validate_signal_ref(body.signal)
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
                db_path=db_path,
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
            db_path=db_path,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _auto_db_path(name: str, *, global_db_path: str | None) -> str:
    """Derive an isolated per-strategy store path for a UI-deployed strategy.

    Places the store under a ``dashboard/`` subdirectory of the manifest's storage
    directory, keyed by a filename-sanitised ``name``:

    * a global ``storage.db_path`` of ``<dir>/<file>.sqlite`` →
      ``<dir>/dashboard/<name>.sqlite``;
    * a ``None`` global store → ``dashboard/<name>.sqlite`` (relative to the
      process cwd, matching the manifest's own relative paths).

    So a strategy deployed without an explicit ``db_path`` gets its own store by
    default — two deployments in one manifest never commingle their fills. The
    ``name`` is sanitised to a safe filename stem (only alphanumerics, ``-``, ``_``
    and ``.`` survive; anything else becomes ``_``).
    """
    import pathlib

    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name) or "unit"
    base_dir = (
        pathlib.PurePosixPath(global_db_path).parent
        if global_db_path is not None
        else pathlib.PurePosixPath(".")
    )
    return str(base_dir / "dashboard" / f"{safe}.sqlite")


def _sanitise_body_db_path(raw: str) -> str:
    """Confine a client-supplied ``db_path`` to the dashboard data dir (raises otherwise).

    I-3 hardening: the auto-derived store path (:func:`_auto_db_path`) is already
    filename-sanitised, but an **explicit** ``db_path`` in the deploy body used to be
    trusted verbatim — a token holder could pass ``../../../../tmp/evil.sqlite`` or an
    absolute ``/etc/x.sqlite`` and get a write-anywhere SQLite file. This clamps the
    body path to a *relative, traversal-free* location so it always lands under the
    process's data dir (the manifest's own relative root), never outside it:

    * an **absolute** path (POSIX ``/…`` or a Windows drive / UNC) is rejected;
    * any ``..`` path component (traversal) is rejected;
    * a benign relative path (incl. a leading ``./``) passes through **verbatim**
      (no silent rewrite) so ``"./var/custom/pf.sqlite"`` is stored as given.

    Parameters
    ----------
    raw : str
        The explicit ``db_path`` from the deploy body.

    Returns
    -------
    str
        The confined path, verbatim (whitespace-stripped).

    Raises
    ------
    HTTPException
        ``400`` if the path is absolute, empty, or contains a ``..`` traversal.
    """
    import ntpath

    candidate = (raw or "").strip()
    # Reject POSIX-absolute (`/x`), Windows-absolute / drive-relative (`C:\`, `\x`)
    # and UNC paths up front — never let the store escape the data dir via an
    # absolute root. `ntpath.isabs` catches the Windows shapes `posixpath` misses.
    if not candidate or candidate.startswith(("/", "\\")) or ntpath.isabs(candidate):
        raise HTTPException(
            status_code=400,
            detail="db_path must be a relative path under the dashboard data dir",
        )
    # Split on both separators so `..\\..` (Windows-style) is caught too, then reject
    # any traversal component. A single leading `.` is fine (current dir).
    parts = candidate.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise HTTPException(
            status_code=400,
            detail="db_path must not traverse outside the dashboard data dir ('..')",
        )
    return candidate


def _validate_signal_ref(ref: str) -> None:
    """Gate a deploy-body ``signal.ref`` before it reaches ``import_module`` (I-1).

    A dotted ``"module:function"`` ref is imported at unit start via
    :func:`importlib.import_module` — for a token holder that is an
    arbitrary-module-import primitive (RCE-adjacent: importing a module runs its
    top level). This validates the ref at the HTTP boundary so a disallowed one is a
    ``400`` (nothing imported), *before* the supervisor is asked to build the unit:

    * a **builtin** name (no ``":"``, e.g. ``"ma_crossover"``) is not an import path
      — it maps to :data:`~trading_bot.application.run_app._BUILTIN_SIGNALS` — so it
      passes the import gate here (its own validity is checked when the unit builds);
    * a **dotted** ``"module:function"`` ref must have a non-empty module + function,
      the module part must be a dotted identifier (:data:`_SIGNAL_REF_MODULE_RE`),
      and its first segment must be in :data:`_SIGNAL_REF_ALLOWED_PREFIXES`. Anything
      else is rejected.

    Residual blast radius (documented, not eliminated): this confines the *import
    root* to the trading stack's own packages, but any importable module **under**
    those prefixes still runs its top-level code on import, and the named callable is
    then invoked with client-supplied ``params``. The allow-list narrows *which*
    packages a token holder can pull in; it is not a sandbox. The real trust boundary
    remains the auth token — treat dashboard write access as code-execution-adjacent.

    Parameters
    ----------
    ref : str
        The deploy body's ``signal`` ref (builtin name or ``"module:function"``).

    Raises
    ------
    HTTPException
        ``400`` if a dotted ref is malformed or its module is outside the allow-list.
    """
    if ":" not in ref:
        # A builtin name — not an import path; the unit-build step validates it.
        return
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr or not _SIGNAL_REF_MODULE_RE.match(module_name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"signal ref {ref!r} must be a dotted 'module:function' "
                "(dotted-identifier module, non-empty function)"
            ),
        )
    root = module_name.split(".", 1)[0]
    if root not in _SIGNAL_REF_ALLOWED_PREFIXES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"signal ref module {module_name!r} is not in the allow-list "
                f"{_SIGNAL_REF_ALLOWED_PREFIXES}; deploy refused"
            ),
        )


def _discover_signals() -> dict[str, list[str]]:
    """List the deployable signal refs — builtins + a scan of ``strategies/``.

    Best-effort discovery for the UI's deploy form:

    * ``builtins`` — the single-instrument builtin names from
      :data:`~trading_bot.application.run_app._BUILTIN_SIGNALS` (e.g.
      ``ma_crossover``).
    * ``discovered`` — every ``strategies/*/signal.py`` module scanned for
      module-level callables whose name ends with ``_signal`` (the
      portfolio-signal wrapper shape, e.g. ``my_portfolio_signal`` /
      ``my_kraken_signal``), returned as ``"module:function"`` refs (e.g.
      ``"strategies.<yourpkg>.signal:my_portfolio_signal"``).

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


#: Health severity ranking — the basis of `/api/health`'s `worst` aggregation
#: (:func:`_worst_health`): the highest-ranked value among running units' own
#: `health` wins.
_HEALTH_RANK = {"ok": 0, "warn": 1, "error": 2}


def _worst_health(healths: Iterable[str]) -> str:
    """The worst of ``healths`` by severity rank; ``"ok"`` when ``healths`` is empty.

    An empty ``healths`` means no running unit to report on — no unit failing
    beats no unit at all, so the aggregate degrades to ``"ok"`` rather than an
    arbitrary sentinel.
    """
    worst = "ok"
    for health in healths:
        if _HEALTH_RANK[health] > _HEALTH_RANK[worst]:
            worst = health
    return worst


def _status_dict(status: StrategyStatus, config: AppConfig) -> dict[str, Any]:
    """Render a :class:`StrategyStatus` for JSON (money as exact Decimal string).

    ``last_eval_ts`` / ``last_asof_ts`` are already integer epoch ms (or
    ``None``) on the domain side — no stringification needed, unlike the money
    fields.

    ``display_currency`` (resolved for the unit's ``exchange``) +
    ``total_value_display`` / ``unrealised_display`` (converted from the
    unit's ``quote``, mirroring :func:`_position_row_dict`'s pair) are
    additive — ``None`` (JSON ``null``) when no rate is declared for that
    quote (or the quote is ``None`` — a mixed-quote portfolio), never a
    guessed conversion.
    """
    return {
        "name": status.name,
        "kind": status.kind,
        "exchange": status.exchange,
        "span": status.span,
        "quote": status.quote,
        "mode": status.mode,
        "running": status.running,
        "realised_pnl": (
            str(status.realised_pnl) if status.realised_pnl is not None else None
        ),
        "open_orders": status.open_orders,
        "last_eval_ts": status.last_eval_ts,
        "last_asof_ts": status.last_asof_ts,
        "allocation": _money_str(status.allocation),
        "contributed": _money_str(status.contributed),
        "unrealised": _money_str(status.unrealised),
        "total_value": _money_str(status.total_value),
        "capital_policy": status.capital_policy,
        "health": status.health,
        "health_detail": list(status.health_detail),
        "display_currency": resolve_currency(status.exchange, config),
        "total_value_display": _money_str(
            convert(status.total_value, status.quote, config)
        ),
        "unrealised_display": _money_str(
            convert(status.unrealised, status.quote, config)
        ),
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
    the typed acknowledgement — the body's ``ack`` must equal
    :data:`_LIVE_ACK_PHRASE` (enforced server-side, not just in the browser); a bare
    ``{"mode":"live","confirm":true}`` without it returns **403**, changing nothing.
    The supervisor's own ``confirm_live`` gate is the second line of defence (a
    :class:`~trading_bot.domain.errors.LiveTradingNotEnabled` there is also **403**).

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
        """List every managed strategy with its exchange / mode / running / PnL.

        ``last_asof_ts`` is the as-of (epoch ms) of the last **completed**
        evaluation — the latest bar time the strategy actually computed on, as
        opposed to ``last_eval_ts`` (the wall-clock of the last *attempted*
        tick). ``None`` before the first completed evaluation. This is the field
        the future "last bar → next bar" timing chip (dashboard-tables-ux) reads;
        it needs no further server change.
        """
        sup = _sup(request)
        config = sup.manifest()
        return [_status_dict(s, config) for s in sup.status()]

    @app.post("/api/strategies/{name}/start")
    async def start_strategy(name: str, request: Request) -> dict[str, Any]:
        _guard_write()
        sup = _sup(request)
        try:
            await sup.start(name)
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (BrokerError, LiveTradingNotEnabled) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "status": _status_dict(sup.status(name)[0], sup.manifest())}

    @app.post("/api/strategies/{name}/stop")
    async def stop_strategy(name: str, request: Request) -> dict[str, Any]:
        _guard_write()
        sup = _sup(request)
        try:
            await sup.stop(name)
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "status": _status_dict(sup.status(name)[0], sup.manifest())}

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
        # Server-side live gate (I-2): a live switch requires the typed
        # acknowledgement phrase in the body, not just `confirm:true`. The browser
        # modal makes the operator type it (UX), but the enforcement lives HERE so a
        # raw POST bypassing the UI cannot flip to live. Constant-time compare (the
        # phrase is not a secret, but this avoids a length/prefix oracle).
        if body.mode == "live":
            ack = body.ack or ""
            if not secrets.compare_digest(ack, _LIVE_ACK_PHRASE):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "switching to live requires the typed acknowledgement "
                        f"{_LIVE_ACK_PHRASE!r} in the request body's 'ack' field"
                    ),
                )
        sup = _sup(request)
        try:
            await sup.set_mode(
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
        return {"ok": True, "status": _status_dict(sup.status(name)[0], sup.manifest())}


def create_control_app(
    supervisor: StrategySupervisor, *, auth_token: str | None = None
) -> FastAPI:
    """Build the daemon's control dashboard — now a thin alias of the unified one.

    **Retired split (kept as a backward-compat wrapper).** The standalone control
    app has been fully subsumed by :func:`create_dashboard_app`: the unified
    dashboard is *also* the control plane (it lists the managed strategies and lets
    a client start / stop them, switch mode, deploy / remove — all behind the same
    safety gates). This factory now just delegates to
    ``create_dashboard_app(supervisor, auth_token=auth_token)`` so existing
    imports / callers (``start --serve``, tests) keep working while there is a
    single implementation and one set of gates.

    **Real money is gated** (unchanged): switching a strategy to ``live`` requires
    the typed acknowledgement (``ack`` == :data:`_LIVE_ACK_PHRASE`) in the request
    body, enforced server-side; otherwise the endpoint returns **403** and nothing
    changes. The factory's credential + risk-limit gates still apply when a live unit
    is actually started.

    **Authentication (for remote exposure)** is the dashboard's token login: with
    ``auth_token`` set, ``/login`` exchanges the token for an HttpOnly session
    cookie, an auth-guard middleware refuses unauthenticated requests, and login is
    rate-limited. With ``auth_token`` ``None`` (default) there is **no** auth — only
    safe behind loopback / an SSH tunnel.

    Parameters
    ----------
    supervisor : StrategySupervisor
        The supervisor to expose, read+controlled through the unified dashboard.
    auth_token : str or None, optional
        When set, require this token to log in (enables the auth guard). ``None``
        (default) leaves the app unauthenticated — loopback / tunnel only.

    Returns
    -------
    FastAPI
        The unified dashboard application (serve via uvicorn, or a ``TestClient``).

    """
    return create_dashboard_app(supervisor, auth_token=auth_token)


def _install_control_auth(
    app: FastAPI, *, auth_token: str, templates: Jinja2Templates | None
) -> None:
    """Gate ``app`` behind a token login — for remote exposure (dccd-style).

    Adds an in-memory session store, an auth-guard middleware (``401`` for
    ``/api/*``, redirect to ``/login`` for pages — except the open prefixes), a
    rate-limited ``/login`` (token → HttpOnly session cookie) and ``/logout``.
    ``/api/*`` also accepts a ``Bearer <token>`` header or ``?token=`` query for
    non-browser clients. Constant-time token comparison. Sessions are in-process
    (reset on restart — fine for a single daemon).

    Hardening baked in
    ------------------
    * **Bounded state (I-7).** Sessions are pruned by TTL *and* capped at
      :data:`_MAX_SESSIONS` (oldest evicted); the login rate-buckets are swept of
      idle entries (:data:`_RATE_BUCKET_TTL_SECONDS`) so neither map grows without
      bound over a long-lived daemon.
    * **Trusted-peer rate key (I-10).** The login throttle keys on the transport
      peer (``request.client.host``), **not** a client-supplied ``X-Forwarded-For``
      — correct for the direct-uvicorn tailnet (spoofing XFF cannot reset the
      bucket). *Assumption: no reverse proxy.* Behind a proxy every client would
      collapse to the proxy address and share one bucket; if a proxy is adopted,
      switch to a trusted-XFF parse gated on a proxy allowlist.
    * **``Secure`` cookie posture (I-4).** ``X-Forwarded-Proto`` is **not** trusted
      by default (:data:`_TRUST_FORWARDED_PROTO`): on the plain-HTTP tailnet the
      header is client-controlled and there is no TLS, so ``Secure`` follows only a
      genuine ``https`` scheme. WireGuard — not this flag — is the transport
      security layer.
    * **CSRF on the login flow (I-13).** ``/login`` / ``/logout`` carry a
      double-submit CSRF token (cookie + hidden field), and the session cookie is
      ``SameSite=strict`` — so a cross-site POST can neither carry the cookie nor
      forge the CSRF pair. The mutating ``/api/*`` routes are already protected by
      SameSite + same-origin + the JSON content-type preflight.
    """
    import time

    from fastapi.responses import RedirectResponse

    app.state.sessions = {}  # sid -> created_ns
    app.state.login_buckets = {}  # client -> (tokens, last monotonic)

    def _prune() -> None:
        # I-7: sweep expired sessions AND idle rate-buckets so neither map grows
        # without bound. Sessions past their TTL are dropped; a rate-bucket idle
        # longer than its TTL has fully refilled (dropping it changes no decision).
        now_ns = time.time_ns()
        cutoff = now_ns - _SESSION_TTL_SECONDS * 1_000_000_000
        for sid in [s for s, ts in app.state.sessions.items() if ts < cutoff]:
            app.state.sessions.pop(sid, None)
        now_mono = time.monotonic()
        buckets = app.state.login_buckets
        for key in [
            k
            for k, (_, last) in buckets.items()
            if now_mono - last > _RATE_BUCKET_TTL_SECONDS
        ]:
            buckets.pop(key, None)

    def _new_session() -> str:
        _prune()
        # I-7: cap live sessions — evict the oldest if at the ceiling before minting
        # a new one, so a login storm cannot grow the map past the bound.
        while len(app.state.sessions) >= _MAX_SESSIONS:
            oldest = min(app.state.sessions, key=app.state.sessions.get)
            app.state.sessions.pop(oldest, None)
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
        # I-4: only a genuine `https` scheme sets `Secure` — a client-supplied
        # `X-Forwarded-Proto` is trusted only when a proxy mode is explicitly
        # enabled (`_TRUST_FORWARDED_PROTO`), never on the plain-HTTP tailnet where
        # the header is forgeable and forcing `Secure` would self-break the cookie.
        if request.url.scheme == "https":
            return True
        if _TRUST_FORWARDED_PROTO:
            fwd = request.headers.get("x-forwarded-proto", "")
            return fwd.split(",", 1)[0].strip() == "https"
        return False

    def _safe_next(nxt: str | None) -> str:
        if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
            return nxt
        return "/"

    def _rate_key(request: Request) -> str:
        # I-10: key on the transport peer, NOT a client-supplied X-Forwarded-For —
        # correct for the direct-uvicorn tailnet (XFF spoofing cannot reset the
        # bucket). Assumes no reverse proxy; behind one, switch to a trusted-XFF
        # parse gated on a proxy allowlist (see the factory docstring).
        client = request.client
        return client.host if client else "unknown"

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
        # I-13: double-submit CSRF — the POST must echo the form field AND the
        # cookie, and they must match. REUSE the browser's existing cookie when it
        # has the shape we mint: any background request landing on /login (e.g. an
        # unauthenticated asset fetch redirected here) re-renders this page, and
        # minting a fresh token per render would rotate the cookie *under* the form
        # the user is already looking at — every submit would then 403. A stable
        # per-browser token is the standard double-submit pattern.
        csrf = request.cookies.get(_CSRF_COOKIE, "")
        if not _CSRF_SHAPE.match(csrf):
            csrf = secrets.token_urlsafe(32)
        if templates is not None:
            resp: Any = templates.TemplateResponse(
                request,
                "login.html",
                {
                    "version": trading_bot.__version__,
                    "next": nxt,
                    "error": error,
                    "csrf": csrf,
                    "csrf_field": _CSRF_FIELD,
                },
                status_code=status,
            )
        else:
            resp = HTMLResponse(
                '<form method="post" action="/login">'
                f'<input type="hidden" name="next" value="{nxt}">'
                f'<input type="hidden" name="{_CSRF_FIELD}" value="{csrf}">'
                '<input name="token" type="password" placeholder="token">'
                "<button>Sign in</button></form>",
                status_code=status,
            )
        resp.set_cookie(
            _CSRF_COOKIE,
            csrf,
            httponly=False,
            samesite="strict",
            secure=_is_https(request),
            max_age=_SESSION_TTL_SECONDS,
        )
        return resp

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Any:
        """Serve the browser's default icon probe (open route, see _OPEN_PREFIXES)."""
        return RedirectResponse("/static/favicon.svg", status_code=308)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Any:
        return _login_page(request)

    @app.post("/login")
    async def login_submit(request: Request) -> Any:
        if not _rate_allow(_rate_key(request)):
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
        # I-13: double-submit CSRF check — the form field must equal the cookie the
        # GET set (constant-time compare, both must be present and non-empty). A
        # cross-site login-CSRF cannot read the SameSite=strict cookie to forge it.
        csrf_cookie = request.cookies.get(_CSRF_COOKIE, "")
        csrf_form = (form.get(_CSRF_FIELD) or [""])[0]
        if (
            not csrf_cookie
            or not csrf_form
            or not secrets.compare_digest(csrf_form, csrf_cookie)
        ):
            return _login_page(
                request, error="Invalid or missing CSRF token.", status=403
            )
        if not secrets.compare_digest(token, auth_token):
            return _login_page(request, error="Invalid token.", status=401)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(
            _SESSION_COOKIE,
            _new_session(),
            httponly=True,
            samesite="strict",
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
    ("/strategies/new", "strategies_new.html"),
    ("/orders", "orders.html"),
    ("/logs", "logs.html"),
)

#: The dimensions ``/api/positions`` and ``/api/orders`` can be grouped by. A row
#: carries a ``strategy``, ``exchange`` and (positions only) a ``base`` crypto tag;
#: these are the group-by keys the Overview's controls offer.
_GROUP_BY_KEYS: tuple[str, ...] = ("crypto", "exchange", "strategy")

#: The default cap on how many history rows ``/api/orders`` and ``/api/fills``
#: return (most recent first). Keeps the Orders page responsive on a long history.
_HISTORY_LIMIT_DEFAULT = 200

#: The KPI aggregation levels ``/api/kpi`` accepts (see
#: :meth:`~trading_bot.application.supervisor.StrategySupervisor.kpi`).
_KPI_LEVELS: tuple[str, ...] = ("strategy", "exchange", "total")

#: The ``?mode=`` filters ``/api/pnl`` accepts — the three deployment modes plus
#: ``"all"`` (every mode present). Live / testnet stay separate series.
_PNL_MODES: tuple[str, ...] = ("all", "paper", "testnet", "live")


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


def _filtered(
    rows: list[dict[str, Any]],
    *,
    crypto: str | None,
    exchange: str | None,
    strategy: str | None,
) -> list[dict[str, Any]]:
    """Keep only the rows matching every set ``crypto`` / ``exchange`` / ``strategy``.

    The Orders/Fills page's server-side filter, mirroring the tags every serialized
    row carries (``base`` for crypto, ``exchange``, ``strategy``). An unset filter
    (``None``) matches everything; each set filter is an exact, case-insensitive
    match against its column. A row must satisfy **all** set filters (AND) to pass.
    """
    wanted = (
        ("base", crypto),
        ("exchange", exchange),
        ("strategy", strategy),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        if all(
            value is None or str(row.get(field, "")).lower() == value.lower()
            for field, value in wanted
        ):
            out.append(row)
    return out


def create_dashboard_app(
    supervisor: StrategySupervisor,
    *,
    auth_token: str | None = None,
    read_only: bool = False,
    on_change: Callable[[], None] | None = None,
    schedule_info: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    """Build the **unified dashboard** FastAPI over a :class:`StrategySupervisor`.

    The foundation that hosts both monitoring *and* control in one app and one
    shell (``base.html``): a top nav across Overview / Strategies / Orders / Logs
    (with the per-strategy detail page ``/strategies/{name}`` under Strategies), a
    brand + version + health chip + connection dot. This factory ships the
    **shell + stub pages + health** — the per-page data (positions, orders, PnL,
    logs) lands in later leaves, fetched client-side over ``/api/*``.

    Every page renders a server-side *shell only* (version + ``read_only`` + auth
    flags); no supervisor data is rendered server-side, so the pages are pure HTTP
    clients of the API. ``GET /api/health`` reports liveness + a snapshot of what
    the supervisor manages (``{status, mode, strategies, read_only, next_tick_ts,
    tick}``).

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
    schedule_info : Callable[[], dict] or None, optional
        A hook returning ``{"next_tick_ts": <epoch ms int or None>, "tick": <str
        or None>}`` — the daemon's scheduler cadence, surfaced on ``/api/health``.
        Keeps this app **scheduler-agnostic**: only the daemon (``_run_daemon``)
        has an ``apscheduler`` job to report, so it injects this hook; the plain
        ``dashboard`` command (no scheduler) passes ``None`` and both fields stay
        ``null``. The hook is called under a ``try``/``except`` — a raising or
        absent hook degrades to ``null``/``null``, never breaking health.

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
    _install_hardening(app)  # body-size cap + security headers (I-6, I-11)
    app.state.supervisor = supervisor
    app.state.read_only = read_only
    app.state.auth_enabled = bool(auth_token)
    app.state.on_change = on_change
    app.state.schedule_info = schedule_info

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

        # The per-strategy detail page — a parameterized route the tuple above
        # cannot express (it carries a `{name}` path param). Registered AFTER the
        # `/strategies/new` shell (in the tuple, so it binds first) so the exact
        # "new" is never mistaken for a strategy name. Still a pure shell: only the
        # `name` is injected server-side (404 for an unknown unit — checked against
        # the supervisor's names); every engine datum is client-fetched from /api/*.
        @app.get(
            "/strategies/{name}",
            response_class=HTMLResponse,
            name="page:strategy-detail",
        )
        async def strategy_detail(name: str, request: Request) -> Any:
            if name not in _sup(request).names():
                raise HTTPException(
                    status_code=404, detail=f"unknown strategy {name!r}"
                )
            return templates.TemplateResponse(
                request,
                "strategy_detail.html",
                {
                    # Highlight the Strategies tab (the detail page lives under it).
                    "active": "/strategies",
                    "strategy_name": name,
                    "version": trading_bot.__version__,
                    "read_only": request.app.state.read_only,
                    "auth": request.app.state.auth_enabled,
                },
            )

    # `/pnl` retired INTO the per-strategy detail page (its equity chart + per-mode
    # stats now live on `/strategies/{name}`). Redirect so an old bookmark still
    # lands somewhere sensible — the Overview. Registered unconditionally (a
    # redirect needs no templates) so the bookmark never 404s.
    @app.get("/pnl", include_in_schema=False)
    async def pnl_redirect() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        """Liveness + a snapshot of what the supervisor manages.

        ``next_tick_ts`` (epoch ms) / ``tick`` (human trigger description) come
        from the ``schedule_info`` hook when one was injected (the daemon's
        cadence); both stay ``null`` with no hook, and a hook that raises
        degrades to ``null``/``null`` too — health must never 500 because the
        scheduler hiccuped.

        ``worst`` is the worst per-unit :attr:`~trading_bot.application.
        supervisor.StrategyStatus.health` among **running** units
        (:func:`_worst_health`; ``"ok"`` with none running — no unit failing
        beats no unit at all). ``unhealthy`` counts every unit (running or
        stopped — a stopped unit still carries its last cached accounting
        report) whose ``health`` is not ``"ok"``.
        """
        sup = _sup(request)
        next_tick_ts: int | None = None
        tick: str | None = None
        hook = request.app.state.schedule_info
        if hook is not None:
            try:
                info = hook() or {}
                next_tick_ts = info.get("next_tick_ts")
                tick = info.get("tick")
            except Exception:  # noqa: BLE001 - health must degrade, never 500
                next_tick_ts = None
                tick = None
        statuses = sup.status()
        worst = _worst_health(s.health for s in statuses if s.running)
        unhealthy = sum(1 for s in statuses if s.health != "ok")
        return {
            "status": "ok",
            "mode": sup.mode,
            "strategies": len(sup.names()),
            "read_only": request.app.state.read_only,
            "next_tick_ts": next_tick_ts,
            "tick": tick,
            "worst": worst,
            "unhealthy": unhealthy,
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
        sup = _sup(request)
        config = sup.manifest()
        rows = [_position_row_dict(row, config) for row in sup.positions()]
        return _grouped(rows, group_by)

    # -- Balances (broker-reported free balances, one row per running unit) -- #

    @app.get("/api/balances")
    async def balances(
        request: Request, strategy: str | None = None
    ) -> list[dict[str, Any]]:
        """Broker-reported free balances, one row per running unit.

        A read (safe under ``read_only``). Each row is ``{strategy, exchange,
        mode, balances: {asset: "exact-decimal"}, error}`` — the venue's own view
        of what it holds (see :meth:`~trading_bot.application.supervisor.
        StrategySupervisor.balances`), as opposed to the locally-tracked
        exposure ``/api/positions`` reports. A stopped unit contributes nothing.
        A broker error degrades that unit's row to an empty ``balances`` dict
        plus a non-``null`` ``error`` string — **HTTP 200**, never a 500, so the
        dashboard can poll this endpoint safely through a transient venue outage.
        ``?strategy=`` filters to one unit (mirroring ``/api/orders``'s /
        ``/api/fills``'s filter). Money is rendered as exact Decimal strings.

        This is the prerequisite seam for the positions<->balances cross-check
        (an accounting-guardrail extension) and the canary-roundtrip live oracle
        (roadmap #6): both need the venue's own reported balances, not just the
        engine's locally-tracked positions.
        """
        rows = [_balance_row_dict(row) for row in await _sup(request).balances()]
        return _filtered(rows, crypto=None, exchange=None, strategy=strategy)

    # -- Orders (open + history, aggregated, groupable + filterable) --------- #

    @app.get("/api/orders")
    async def orders(
        request: Request,
        group_by: str | None = None,
        history: bool = False,
        limit: int = _HISTORY_LIMIT_DEFAULT,
        crypto: str | None = None,
        exchange: str | None = None,
        strategy: str | None = None,
    ) -> Any:
        """Orders across the units — open by default, **open + recent history** with
        ``?history=true``.

        The Overview's open-orders strip reads this flat (``?history=false``, the
        default: non-terminal orders on running units). The Orders page reads
        ``?history=true`` for the full order list (any status, running **and**
        stopped units, each unit's most-recent ``?limit=`` capped), filtered by
        ``?crypto=&exchange=&strategy=`` (the same tags ``/api/positions`` carries).
        ``?group_by=crypto|exchange|strategy`` buckets the result. Money is exact
        Decimal strings.
        """
        if group_by is not None and group_by not in _GROUP_BY_KEYS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown group_by {group_by!r}; expected one of {_GROUP_BY_KEYS}",
            )
        sup = _sup(request)
        source = sup.order_history() if history else sup.open_orders()
        rows = [_order_row_dict(row) for row in source]
        rows = _filtered(rows, crypto=crypto, exchange=exchange, strategy=strategy)
        # History is capped to the most recent `limit` (the source is oldest-first,
        # so take the tail); the open-orders view is small and left uncapped.
        if history and limit >= 0:
            rows = rows[-limit:] if limit else []
        return _grouped(rows, group_by)

    # -- Fills (confirmed executions history, filterable) -------------------- #

    @app.get("/api/fills")
    async def fills(
        request: Request,
        group_by: str | None = None,
        limit: int = _HISTORY_LIMIT_DEFAULT,
        crypto: str | None = None,
        exchange: str | None = None,
        strategy: str | None = None,
    ) -> Any:
        """Confirmed fill history across every unit's store — the PnL source of truth.

        The Orders/Fills page's fill table: every unit's persisted fills (running
        **and** stopped), tagged with ``strategy`` / ``exchange`` / ``base`` crypto,
        filtered by ``?crypto=&exchange=&strategy=`` (mirroring ``/api/positions``'s
        tagging) and capped to the most recent ``?limit=`` (default
        :data:`_HISTORY_LIMIT_DEFAULT`). ``?group_by=crypto|exchange|strategy``
        buckets the result. Money is exact Decimal strings; a read (safe under
        ``read_only``).
        """
        if group_by is not None and group_by not in _GROUP_BY_KEYS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown group_by {group_by!r}; expected one of {_GROUP_BY_KEYS}",
            )
        rows = [_fill_row_dict(row) for row in _sup(request).fills()]
        rows = _filtered(rows, crypto=crypto, exchange=exchange, strategy=strategy)
        # Most-recent-first cap (the source is oldest-first execution order).
        if limit >= 0:
            rows = rows[-limit:] if limit else []
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
        sup = _sup(request)
        config = sup.manifest()
        return [
            _kpi_row_dict(row, config)
            for row in sup.kpi(level)  # type: ignore[arg-type]
        ]

    # -- PnL series (per-mode realised-PnL / equity curve over time) --------- #

    @app.get("/api/pnl")
    async def pnl(request: Request, strategy: str, mode: str = "all") -> dict[str, Any]:
        """Per-mode realised-PnL / equity curve for one strategy, over time.

        ``?strategy=<name>`` (required) — the derived equity curve per mode
        (``{mode: [[ts_ms, pnl, equity], ...]}``) folded from the strategy's
        confirmed fills, plus ``v0`` and a current end point per mode
        (``{equity, unrealised}``). **Live and testnet stay separate series**
        (testnet is fake money — never combined). ``?mode=live|testnet|paper|all``
        (default ``all``) filters to a single mode. Money as exact Decimal
        strings; ``ts_ms`` integer. An unknown ``strategy`` is a 404; a strategy
        with no fills is an empty series (200, not an error).

        Carries no ``last_asof_ts`` of its own — each series point already
        carries its own ``ts_ms``, so there is no separate "as of" to surface
        (unlike ``/api/strategies``, a single evaluation-cadence snapshot).
        """
        from trading_bot.domain.errors import ConfigError

        if mode not in _PNL_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown mode {mode!r}; expected one of {_PNL_MODES}",
            )
        try:
            result = _sup(request).pnl_series(strategy)
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        only = None if mode == "all" else mode
        return _pnl_series_dict(result, only_mode=only)

    # -- SSE events (merged across every unit's engine bus) ------------------ #

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        """Server-Sent-Events fanning **every** running unit's bus onto one feed.

        Registers a fresh queue on each running unit's engine
        :class:`~trading_bot.application.events.EventBus` and multiplexes them onto
        a single generator, yielding each event as a ``data: <json>\\n\\n`` frame
        (money as Decimal strings, tagged with a ``type``, the emitting unit's
        ``strategy`` name, and a server ``ts`` epoch-ms — the Logs page's
        attribution + timestamp). Order and fill events are de-duplicated by
        their domain id so an execution seen on two buses is emitted once. Every
        registered queue is unregistered in a ``finally`` on disconnect.
        Read-only — subscribing observes; it never trades.
        """
        sup = _sup(request)
        # Snapshot the running units' (name, bus) pairs now; the merged stream is
        # over the set live at connect time (a unit started later is picked up on
        # reconnect, like the single-engine SSE view). The name travels alongside
        # its bus/queue so every frame can be tagged with the emitting strategy.
        units = [
            (unit.name, unit.engine.bus)
            for unit in sup._running_units()  # noqa: SLF001 — read the wired buses
            if unit.engine is not None
        ]
        names = [name for name, _ in units]
        buses = [bus for _, bus in units]
        queues = [bus.add_queue() for bus in buses]

        async def _generator() -> Any:
            seen: set[str] = set()
            # Tracks the current iteration's per-queue getter tasks so a
            # cancellation between iterations (see the CancelledError handler
            # below) can still cancel whichever ones are outstanding.
            getters: list[asyncio.Task[Any]] = []
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
                    # periodically re-checks disconnection and heartbeats). Rebuilt
                    # every iteration, so map each fresh getter task back to its
                    # unit name for the frame tag below.
                    getters = [asyncio.ensure_future(q.get()) for q in queues]
                    task_name = dict(zip(getters, names, strict=True))
                    done, pending = await asyncio.wait(
                        getters,
                        timeout=15.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    getters = []
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
                        frame = {
                            **_event_dict(event),
                            "strategy": task_name[task],
                            "ts": _epoch_ms(),
                        }
                        yield f"data: {json.dumps(frame, default=_default)}\n\n"
            except asyncio.CancelledError:
                # Server shutdown (the graceful-shutdown timeout force-cancels this
                # task while a client holds the merged stream open) or the ASGI
                # server tearing the connection down: the stream is over, which is
                # the correct time for this generator to end, not an error. Any
                # getter task still outstanding at the point of cancellation (e.g.
                # cancelled mid-`asyncio.wait`, before the per-iteration cleanup
                # above ran) is cancelled here too, so no bare queue.get() task is
                # left dangling. Left uncaught, this propagates as a bare
                # CancelledError that uvicorn/starlette log as a scary ERROR-level
                # traceback on every shutdown while a client holds this endpoint
                # open.
                for task in getters:
                    if not task.done():
                        task.cancel()
            finally:
                for bus, queue in zip(buses, queues, strict=True):
                    bus.remove_queue(queue)

        return StreamingResponse(_generator(), media_type="text/event-stream")

    # -- Signal discovery (deployable refs for the create form) -------------- #

    @app.get("/api/signals")
    async def signals(request: Request) -> dict[str, list[str]]:
        """Deployable signal refs — builtins + a scan of ``strategies/*/signal.py``.

        A read (safe under ``read_only``): ``{builtins: [...], discovered:
        ["strategies.<yourpkg>.signal:my_portfolio_signal", ...]}``. The UI picks
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
        # Isolate this deployment's store by default: use an explicit body.db_path
        # if given (sanitised — I-3: a raw body path used to be write-anywhere), else
        # auto-assign one under the manifest's storage dir keyed by name (so two
        # UI-deployed strategies never commingle their fills). The assigned path
        # round-trips into the persisted manifest via the entry.
        db_path = (
            _sanitise_body_db_path(body.db_path)
            if body.db_path
            else _auto_db_path(body.name, global_db_path=sup.manifest().storage.db_path)
        )
        entry = _entry_from_body(body, db_path=db_path)
        try:
            name = sup.add_unit(entry)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # I-9: the supervisor seeds every new unit from the manifest's global mode
        # (`_mode_of`), NOT from a per-entry `body.mode` — so a `body.mode` that the
        # server would silently discard is a misleading contract. Honour it by
        # *rejecting* a mismatch: a deployed unit whose actual seed mode differs from
        # what the body requested is rolled back (nothing left added) with a clear
        # error. So `mode` is meaningful (a testnet/live request off a paper manifest
        # fails loudly instead of quietly seeding paper). Switching a deployed unit's
        # mode afterwards still goes through the live-gated `.../mode` route.
        seeded = sup.status(name)[0].mode
        if seeded != body.mode:
            sup.remove_unit(name)
            raise HTTPException(
                status_code=422,
                detail=(
                    f"requested mode {body.mode!r} but this manifest seeds new units "
                    f"as {seeded!r} (deployments inherit the manifest's global mode); "
                    f"deploy with mode={seeded!r} and switch via .../mode afterwards"
                ),
            )
        _persist(request)
        return {"ok": True, "status": _status_dict(sup.status(name)[0], sup.manifest())}

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

    # -- Capital control plane: deposit / withdraw / policy (paper) ---------- #

    @app.get("/api/strategies/{name}/capital")
    async def capital(name: str, request: Request) -> dict[str, Any]:
        """A unit's capital breakdown + ledger audit trail (a read; safe read-only).

        ``{allocation, contributed, realised, unrealised, total_value,
        withdrawable, policy, events}`` — money as exact Decimal strings; an
        unknown unit is a 404.

        Carries no ``last_asof_ts``: each ledger ``event`` already carries its
        own ``ts`` (when the deposit/withdrawal happened), and this breakdown is
        not tied to a strategy evaluation cadence the way ``/api/strategies``' /
        ``/api/positions``' marks are.
        """
        from trading_bot.domain.errors import ConfigError

        try:
            breakdown = _sup(request).capital_breakdown(name)
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _capital_breakdown_dict(breakdown)

    @app.post("/api/strategies/{name}/capital")
    async def capital_op(
        name: str, body: _CapitalOpBody, request: Request
    ) -> dict[str, Any]:
        """Deposit into / withdraw from a unit's capital ledger — idempotent by ``op_id``.

        This **never** places / amends / cancels an order — capital ops move
        bookkeeping, not orders (the hard invariant). Re-POSTing the same ``op_id``
        is a no-op that returns the unchanged breakdown. ``422`` on a bad amount /
        an over-limit withdrawal (carrying the exact withdrawable figure); ``404``
        an unknown unit; ``409`` a **live** unit (real-money ops are deferred to
        real-key enablement — paper / testnet are unconstrained); ``403`` when the
        dashboard is read-only.
        """
        from trading_bot.domain.errors import (
            ConfigError,
            LiveCapitalOpsDeferred,
            MoneyError,
            WithdrawalTooLarge,
        )

        if request.app.state.read_only:
            raise HTTPException(
                status_code=403,
                detail="dashboard is read-only; capital control is disabled",
            )
        sup = _sup(request)
        op = sup.deposit if body.action == "deposit" else sup.withdraw
        try:
            breakdown = await op(name, body.amount, op_id=body.op_id, note=body.note)
        except LiveCapitalOpsDeferred as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WithdrawalTooLarge as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, MoneyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _capital_breakdown_dict(breakdown)

    @app.post("/api/strategies/{name}/policy")
    async def set_policy(
        name: str, body: _PolicyBody, request: Request
    ) -> dict[str, Any]:
        """Set a unit's capital-evolution policy (``fixed`` / ``compound``) — hot + persisted.

        Flips the sizing policy live (the running unit reflects it next tick, no
        restart) and **persists the manifest** so it survives a restart. ``404``
        an unknown unit; ``403`` when the dashboard is read-only.
        """
        from trading_bot.domain.errors import ConfigError

        if request.app.state.read_only:
            raise HTTPException(
                status_code=403,
                detail="dashboard is read-only; capital control is disabled",
            )
        sup = _sup(request)
        try:
            breakdown = await sup.set_policy(name, body.policy)
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _persist(request)
        return _capital_breakdown_dict(breakdown)

    # The strategy control surface (list + start/stop/mode) — shared with the
    # control app. Under `read_only`, the write routes return 403 (the reads stay).
    _register_strategy_control(app, read_only=read_only)

    if auth_token:
        _install_control_auth(app, auth_token=auth_token, templates=templates)

    return app
