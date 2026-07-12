"""The canary — a deterministic self-test the platform runs on itself.

The canary is the operator's proof that a wired engine actually round-trips
money correctly on a venue mode, **before** any strategy is trusted with it:
a tiny **sequential round-trip** (market buy *x* → confirm the fill → market
sell *x* → confirm → flat) preceded by two **free probes**, with an **oracle
computed in advance**. Every step's expectation and observation is recorded as
an exact string in a :class:`CanaryReport` — the evidence table — and the run
fails on the first violated expectation while still snapshotting and reporting
everything it measured.

Design (pinned in ``doc/dev/plans/canary-roundtrip/00-plan.md``)
----------------------------------------------------------------
* **Sequential, never simultaneous.** The buy and the sell are placed one
  after the other, each confirmed before the next order is placed. A crossed
  simultaneous pair would trip venue self-trade prevention and make the
  outcome non-deterministic; a sequential round-trip is deterministic on every
  venue mode.
* **Probes before money.** Both probes are *free* (they can never spend):
  they run **before** the round-trip so the expensive step is only reached on
  an engine whose kill path (cancel) and idempotency guarantee have just been
  demonstrated live.

  - **Cancel probe** — place a limit buy far below the market
    (``probe_offset_pct`` percent below the mark, default 50%), cancel it, and
    assert: no fill, no balance move, ``CANCELLED`` persisted in the store.
    This exercises the real kill-switch path end to end.
  - **Idempotency probe** — re-submit an order carrying the **same**
    client-order-id and assert the engine deduped it: the original tracked
    order is returned, no second venue order, no new fill, no second store
    row. This is the invariant that makes a retry safe with real money.
* **Two oracles.** The *paper* oracle (:func:`paper_oracle`, this module) is
  **EXACT** — Decimal equality, no tolerance — because the simulator is fully
  deterministic. The *venue* oracle (leaf 03) can never precompute an absolute
  PnL (spread and drift are not deterministic); it asserts the **accounting
  identity** (our fills == venue-reported fills, our balance deltas ==
  venue-reported deltas, flat) plus a **bounded total cost**. Keeping the two
  oracles distinct is what lets paper be exact without pretending a live venue
  is.
* **Sizing / cost philosophy.** The canary trades the **smallest venue-legal
  size**: the caller sizes ``qty`` from the venue's real minimums
  (``min_qty`` / ``min_notional`` via the instrument-spec resolver) so the
  only money at risk is the fee on a minimum-size round-trip (paper: exactly
  ``2 × fee``; venue: bounded by the configured ``max_cost``). The cancel
  probe never risks even that — but its far-below price shrinks its notional,
  so its quantity is scaled **up** just enough to stay venue-legal at the
  probe price (a resting order's size costs nothing).
* **A dedicated engine only.** The canary trades — it must be impossible to
  point it at a shared book by accident. It therefore refuses an engine
  without its own store (``build_engine`` attaches a store only for an
  explicit ``db_path``; there is no default), and the caller funds the engine
  explicitly (``AppConfig.paper_starting_balances`` on paper). Engine
  construction is the **caller's** job (the CLI leaf); this module drives a
  ready engine through its own seams (``router.submit`` / ``router.cancel``,
  ``broker.balances``, ``tracker``, ``perf``, ``store``) and never constructs
  or mutates wiring.

Paper exactness assumption (documented, relied on by :func:`paper_oracle`)
--------------------------------------------------------------------------
The paper simulator fills **both** market legs at the **same injected mark**,
so the buy and sell notionals cancel exactly and only the fees remain:
realised PnL is exactly ``-(sum of fees)``, the quote balance delta is exactly
``-(sum of fees)`` and the base balance delta is exactly ``0``. On any real
venue none of this holds (spread, drift) — which is precisely why the venue
oracle asserts identities and bounds instead.
"""

from __future__ import annotations

# Built-in
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_UP, localcontext
from typing import TYPE_CHECKING

# Local
from trading_bot.domain.errors import (
    BrokerError,
    MissingOrder,
    OrderError,
    RiskLimitBreached,
)
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import Order, OrderSide, OrderStatus, OrderType

if TYPE_CHECKING:
    from collections.abc import Callable

    from trading_bot.application.service_factory import Engine
    from trading_bot.domain.instrument import Instrument

__all__ = [
    "CanaryCheck",
    "CanaryReport",
    "paper_oracle",
    "run_canary",
    "size_for_minimums",
]

#: Percent denominator for ``probe_offset_pct``.
_HUNDRED: Money = money("100")

#: Default cancel-probe offset: the probe limit buy is placed this many percent
#: below the mark (50% — far enough that no realistic tick ever crosses it).
_DEFAULT_PROBE_OFFSET_PCT: Money = money("50")

#: Precision (significant digits) for the probe-qty scaling division — matches
#: the domain's pinned ``decimal128`` precision (see ``order.AVG_PRICE_PRECISION``).
_PROBE_QTY_PRECISION: int = 34

#: The errors a canary order operation may legitimately surface: they become a
#: failed check (the canary reports, it does not crash), never an escape.
_ORDER_ERRORS = (BrokerError, OrderError, MissingOrder, RiskLimitBreached)


@dataclass(frozen=True, slots=True)
class CanaryCheck:
    """One expectation-vs-observation line of the canary's evidence table.

    Immutable: a check is a recorded fact about one scenario step. ``passed``
    is computed on the underlying **values** (exact ``Decimal`` / enum
    comparisons), never on the rendered strings — two numerically equal
    decimals with different exponents (``-0.10`` vs ``-0.100``) still pass.

    Parameters
    ----------
    name : str
        The check's stable identifier (e.g. ``"cancel_probe.resting"``).
    expected : str
        The expectation, rendered as an exact human-readable string.
    observed : str
        What was actually measured, rendered the same way.
    passed : bool
        Whether the observation met the expectation (value comparison).

    """

    name: str
    expected: str
    observed: str
    passed: bool


@dataclass(slots=True)
class CanaryReport:
    """The canary run's full evidence: ordered checks plus run context.

    Built incrementally by :func:`run_canary`; the context fields (exchange,
    mode, instrument, qty, client-order-ids, balance snapshots) carry enough
    for :meth:`to_text` to print a self-contained evidence table and for
    :func:`paper_oracle` to compute its exact assertions.

    Parameters
    ----------
    exchange : str
        The broker's venue key (``"paper"``, ``"binance"``, ...).
    mode : str
        The engine's configured execution mode (``"paper"`` / ``"live"``).
    instrument : Instrument
        The instrument the canary traded.
    qty : Decimal
        The round-trip quantity (base units) requested per leg.
    probe_cid, buy_cid, sell_cid : str
        The client-order-ids the run minted for its three orders.
    checks : list of CanaryCheck
        The ordered evidence lines, in scenario order (oracle checks last).
    cost : Decimal or None
        The run's total realised cost, ``-(realised PnL)`` — positive means
        the canary paid (fees/spread). ``None`` until the run completes its
        final snapshot.
    initial_balances, final_balances : dict of str to Decimal
        The broker-reported balance snapshots taken before the first order and
        after the last (exact ``Decimal``, keyed by canonical asset code).

    """

    exchange: str
    mode: str
    instrument: Instrument
    qty: Money
    probe_cid: str = ""
    buy_cid: str = ""
    sell_cid: str = ""
    checks: list[CanaryCheck] = field(default_factory=list)
    cost: Money | None = None
    initial_balances: dict[str, Money] = field(default_factory=dict)
    final_balances: dict[str, Money] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Whether the run passed: at least one check, and every check green."""
        return bool(self.checks) and all(check.passed for check in self.checks)

    def to_text(self) -> str:
        """Render the evidence table as stable, exact text (one check per line).

        The output is deterministic for a deterministic run: it contains no
        timestamps and no minted ids — only the run context, each check's
        exact expected/observed strings, the total cost and the verdict.

        Returns
        -------
        str
            The multi-line evidence table.

        """
        lines = [
            f"canary — exchange={self.exchange} mode={self.mode} "
            f"instrument={self.instrument} qty={self.qty}"
        ]
        lines.extend(
            f"[{'PASS' if check.passed else 'FAIL'}] {check.name}: "
            f"expected={check.expected} | observed={check.observed}"
            for check in self.checks
        )
        lines.append(f"cost: {'n/a' if self.cost is None else self.cost}")
        lines.append(f"result: {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def _fmt_balances(balances: dict[str, Money]) -> str:
    """Render a balances mapping as a stable ``ASSET=amount`` line (sorted)."""
    if not balances:
        return "(empty)"
    return " ".join(f"{asset}={amount}" for asset, amount in sorted(balances.items()))


def _probe_qty(instrument: Instrument, qty: Money, probe_price: Money) -> Money:
    """The cancel-probe quantity: ``qty``, scaled up to stay legal at the probe price.

    The probe is priced far below the mark, which shrinks its notional — a
    quantity that is venue-legal for the round-trip at the mark can fall below
    ``min_notional`` at the probe price and be rejected before it ever rests.
    A resting order's size costs nothing (it never fills), so the probe scales
    its quantity **up** to the smallest lot-aligned amount whose notional at
    ``probe_price`` clears the venue minimum (and to ``min_qty`` if that is
    higher). With no ``min_notional`` on the spec (or one already satisfied),
    ``qty`` is used unchanged.

    Parameters
    ----------
    instrument : Instrument
        The (spec-carrying) instrument the probe trades.
    qty : Decimal
        The round-trip quantity the caller sized at the mark.
    probe_price : Decimal
        The probe's (already quantized) far-below limit price.

    Returns
    -------
    Decimal
        The probe quantity: ``qty``, or the smallest venue-legal scale-up.

    """
    min_notional = instrument.min_notional
    if min_notional is None or qty * probe_price >= min_notional:
        required = qty
    else:
        # Divide under an explicit context rounding UP so the scaled quantity's
        # notional can never land a hair below the minimum.
        with localcontext() as ctx:
            ctx.prec = _PROBE_QTY_PRECISION
            ctx.rounding = ROUND_UP
            required = +(min_notional / probe_price)
        step = instrument.qty_step
        if step is not None:
            # Lot-align upward: snap down, then add one lot if that undershot.
            snapped = instrument.quantize_qty(required)
            if snapped < required:
                snapped += step
            required = snapped
    if instrument.min_qty is not None and required < instrument.min_qty:
        required = instrument.min_qty
    return required


def size_for_minimums(instrument: Instrument, price: Money) -> Money:
    """The smallest venue-legal round-trip quantity for ``instrument`` at ``price``.

    Implements the canary's sizing philosophy (module docstring): trade the
    smallest venue-legal size, sized **from scratch** against the venue's real
    minimums (``min_qty`` / ``min_notional``, e.g. via the instrument-spec
    resolver, ``application/instrument_specs.py``) rather than a caller-chosen
    quantity. Unlike :func:`_probe_qty` — which *scales up* an already-legal
    round-trip quantity for a *different* (probe) price — this computes the
    minimum directly: the smallest lot-aligned quantity whose notional at
    ``price`` clears ``min_notional``, raised to ``min_qty`` if that is higher.

    Parameters
    ----------
    instrument : Instrument
        The (spec-carrying) instrument to size. Must carry at least one of
        ``min_qty`` / ``min_notional`` — see Raises.
    price : Decimal
        The reference price (the mark) the notional is computed against. Must
        be strictly positive.

    Returns
    -------
    Decimal
        The smallest venue-legal quantity.

    Raises
    ------
    ValueError
        If ``instrument`` carries neither ``min_qty`` nor ``min_notional`` (an
        unresolved / bare spec — e.g. a degraded resolver fetch, or an
        exchange the resolver does not dispatch): sizing "the smallest legal
        amount" with no real venue minimum to size against would be guessing,
        which the caller must refuse instead of this function silently doing
        so. Also raised if ``price`` is not strictly positive.

    """
    if instrument.min_qty is None and instrument.min_notional is None:
        raise ValueError(
            f"{instrument} carries no venue minimums (min_qty/min_notional): "
            "refusing to guess a venue-legal quantity"
        )
    price = money(price)
    if price <= 0:
        raise ValueError(f"price must be strictly positive, got {price}")

    min_notional = instrument.min_notional
    min_qty = instrument.min_qty
    if min_notional is None:
        # min_qty is guaranteed set here (guarded above) — the lot minimum
        # alone is the smallest legal size with no notional floor to clear.
        assert min_qty is not None
        return min_qty

    # Divide under an explicit context rounding UP so the sized quantity's
    # notional can never land a hair below the minimum (mirrors _probe_qty).
    with localcontext() as ctx:
        ctx.prec = _PROBE_QTY_PRECISION
        ctx.rounding = ROUND_UP
        required = +(min_notional / price)
    step = instrument.qty_step
    if step is not None:
        # Lot-align upward: snap down, then add one lot if that undershot.
        snapped = instrument.quantize_qty(required)
        if snapped < required:
            snapped += step
        required = snapped
    if min_qty is not None and required < min_qty:
        required = min_qty
    return required


def paper_oracle(
    engine: Engine,
    report: CanaryReport,
    *,
    expected_fill_count: int = 2,
) -> list[CanaryCheck]:
    """The EXACT paper oracle: Decimal-equality assertions over the whole run.

    Computable in advance because the simulator is deterministic and fills
    **both round-trip legs at the same injected mark** (see the module
    docstring's exactness assumption): the two notionals cancel exactly and
    only fees remain. The checks, all exact (no tolerance):

    * realised PnL == ``-(sum of fees)`` — fees summed over the **store's**
      persisted fills (fills are the sole PnL truth; the store is their
      persistence), cross-checking the live performance service against what
      was actually recorded;
    * the position is flat (``net_qty == 0``);
    * both round-trip orders are ``FILLED`` with ``filled_qty == qty`` **in
      the store** (not just in memory);
    * the quote balance delta is exactly ``-(sum of fees)`` and the base
      balance delta is exactly ``0`` (same-mark assumption);
    * the round-trip produced exactly ``expected_fill_count`` fills.

    Parameters
    ----------
    engine : Engine
        The canary's dedicated engine (store, tracker, performance service).
    report : CanaryReport
        The completed scenario report — supplies the balance snapshots, the
        round-trip client-order-ids and the quantity.
    expected_fill_count : int, optional
        How many fills the round-trip must have produced. Defaults to ``2``
        (one per leg — the factory paper broker's ``"immediate"`` fill model);
        a caller running a chunked fill model passes its own figure.

    Returns
    -------
    list of CanaryCheck
        The oracle's checks, in the order above.

    Raises
    ------
    ValueError
        If ``engine`` has no store (the oracle's assertions are store-based;
        a canary engine always carries its own dedicated store).

    """
    store = engine.store
    if store is None:
        raise ValueError(
            "paper_oracle requires an engine with its own store: the exact "
            "assertions are checked against persisted rows, not memory"
        )
    store.flush()
    checks: list[CanaryCheck] = []

    # 1. Realised PnL == -(sum of fees), fees from the persisted fills.
    fills = store.fills()
    total_fees = sum((fill.fee for fill in fills), money("0"))
    realised = engine.perf.realised_pnl()
    checks.append(
        CanaryCheck(
            name="oracle.realised_pnl_is_minus_fees",
            expected=f"realised_pnl={-total_fees}",
            observed=f"realised_pnl={realised}",
            passed=realised == -total_fees,
        )
    )

    # 2. Flat position (a never-touched instrument counts as flat).
    position = engine.tracker.position(report.instrument)
    net_qty = position.net_qty if position is not None else money("0")
    checks.append(
        CanaryCheck(
            name="oracle.position_flat",
            expected="net_qty=0",
            observed=f"net_qty={net_qty}",
            passed=net_qty == 0,
        )
    )

    # 3./4. Both round-trip orders FILLED with filled_qty == qty IN THE STORE.
    for name, cid in (
        ("oracle.buy_filled_in_store", report.buy_cid),
        ("oracle.sell_filled_in_store", report.sell_cid),
    ):
        row = store.get_order(cid)
        checks.append(
            CanaryCheck(
                name=name,
                expected=f"status=filled filled_qty={report.qty}",
                observed=(
                    "store row missing"
                    if row is None
                    else f"status={row.status.value} filled_qty={row.filled_qty}"
                ),
                passed=(
                    row is not None
                    and row.status is OrderStatus.FILLED
                    and row.filled_qty == report.qty
                ),
            )
        )

    # 5. Balance deltas: quote moved by exactly -(sum of fees); base by 0.
    quote = report.instrument.symbol.quote
    base = report.instrument.symbol.base
    zero = money("0")
    quote_delta = report.final_balances.get(quote, zero) - report.initial_balances.get(
        quote, zero
    )
    base_delta = report.final_balances.get(base, zero) - report.initial_balances.get(
        base, zero
    )
    checks.append(
        CanaryCheck(
            name="oracle.quote_delta_is_minus_fees",
            expected=f"quote_delta={-total_fees}",
            observed=f"quote_delta={quote_delta}",
            passed=quote_delta == -total_fees,
        )
    )
    checks.append(
        CanaryCheck(
            name="oracle.base_delta_zero",
            expected="base_delta=0",
            observed=f"base_delta={base_delta}",
            passed=base_delta == 0,
        )
    )

    # 6. Fill count: the round-trip produced exactly the expected executions.
    roundtrip_cids = {report.buy_cid, report.sell_cid}
    fill_count = sum(1 for fill in fills if fill.client_order_id in roundtrip_cids)
    checks.append(
        CanaryCheck(
            name="oracle.fill_count",
            expected=f"fills={expected_fill_count}",
            observed=f"fills={fill_count}",
            passed=fill_count == expected_fill_count,
        )
    )
    return checks


async def run_canary(
    engine: Engine,
    instrument: Instrument,
    *,
    qty: Money,
    probe_offset_pct: Money = _DEFAULT_PROBE_OFFSET_PCT,
    run_id: str | None = None,
    oracle: Callable[[Engine, CanaryReport], list[CanaryCheck]] | None = paper_oracle,
) -> CanaryReport:
    """Run the canary scenario on a ready, dedicated ``engine``.

    Drives the engine's **own** seams, in the pinned order: balance snapshot →
    **cancel probe** (far-below limit buy, cancel, assert no fill / no balance
    move / ``CANCELLED`` persisted) → **idempotency probe** (re-submit the same
    client-order-id, assert the engine deduped) → **round-trip** (market buy
    ``qty``, confirm in tracker+store; market sell ``qty``, confirm; assert
    flat) → final balance snapshot → the ``oracle``'s exact checks. Every step
    appends :class:`CanaryCheck`\\ s; **a failed check stops placing further
    orders** (and best-effort cancels anything the run left live) but the
    final snapshot, cost and report still happen — the run fails loudly with
    everything it measured.

    Engine construction (and funding, and sizing ``qty`` from the venue
    minimums) is the caller's job — see the module docstring.

    Parameters
    ----------
    engine : Engine
        The dedicated canary engine. Must carry its **own** store (persistence
        checks are part of the scenario), a broker with a mark for
        ``instrument`` (:meth:`~trading_bot.brokers.base.Broker.ticker`), and
        explicit funding for the round-trip.
    instrument : Instrument
        The (spec-carrying) instrument to trade.
    qty : Decimal
        The round-trip quantity per leg (base units). Must be strictly
        positive, lot-aligned and venue-legal at the mark — the smallest
        venue-legal size, by the sizing philosophy.
    probe_offset_pct : Decimal, optional
        How far below the mark the cancel probe is priced, in percent.
        Defaults to ``50``. Must be strictly inside ``(0, 100)``.
    run_id : str, optional
        The token embedded in the run's client-order-ids
        (``canary-{run_id}-probe/buy/sell``). Defaults to a fresh random
        token, so re-running over the same engine/store never collides ids.
    oracle : callable, optional
        The mode's oracle, called as ``oracle(engine, report)`` **only when
        every scenario check passed** (an aborted run's oracle expectations
        were never measured) and its checks appended to the report. Defaults
        to :func:`paper_oracle`; leaf 03's venue oracle replaces it for
        testnet/live runs. ``None`` skips the oracle.

    Returns
    -------
    CanaryReport
        The full evidence: ordered checks, run context, balance snapshots and
        total realised cost. ``report.passed`` is the verdict.

    Raises
    ------
    ValueError
        If ``engine`` has no store, ``qty`` is not strictly positive, or
        ``probe_offset_pct`` is outside ``(0, 100)`` — caller wiring errors,
        refused before any order is placed.
    BrokerError
        If the broker has no mark price for ``instrument`` (the pre-trade
        ticker read fails) — nothing has been placed yet.

    """
    if engine.store is None:
        raise ValueError(
            "the canary requires an engine with its own dedicated store "
            "(build_engine(..., db_path=...)): persistence checks are part of "
            "the scenario, and a shared/absent store must be impossible"
        )
    qty = money(qty)
    if qty <= 0:
        raise ValueError(f"canary qty must be strictly positive, got {qty}")
    probe_offset_pct = money(probe_offset_pct)
    if not (0 < probe_offset_pct < _HUNDRED):
        raise ValueError(
            f"probe_offset_pct must be in (0, 100), got {probe_offset_pct}"
        )

    broker = engine.broker
    router = engine.router
    store = engine.store
    token = run_id if run_id is not None else uuid.uuid4().hex[:8]
    report = CanaryReport(
        exchange=broker.name,
        mode=engine.config.mode,
        instrument=instrument,
        qty=qty,
        probe_cid=f"canary-{token}-probe",
        buy_cid=f"canary-{token}-buy",
        sell_cid=f"canary-{token}-sell",
    )

    # --- initial snapshot (before any order) --------------------------------- #
    mark = await broker.ticker(instrument)
    report.initial_balances = dict(await broker.balances())
    ok = True

    # --- cancel probe: the free kill-path rehearsal --------------------------- #
    probe_price = instrument.quantize_price(
        mark * (_HUNDRED - probe_offset_pct) / _HUNDRED
    )
    if probe_price <= 0:
        raise ValueError(
            f"cancel-probe price quantized to {probe_price} (mark {mark}, "
            f"offset {probe_offset_pct}%): not a placeable limit price"
        )
    probe_qty = _probe_qty(instrument, qty, probe_price)
    fills_before = len(await broker.fills())
    tracked_probe: Order | None = None
    try:
        tracked_probe = await router.submit(
            Order(
                client_order_id=report.probe_cid,
                instrument=instrument,
                side=OrderSide.BUY,
                qty=probe_qty,
                type=OrderType.LIMIT,
                limit_price=probe_price,
            )
        )
    except _ORDER_ERRORS as exc:
        report.checks.append(
            CanaryCheck(
                name="cancel_probe.resting",
                expected=f"status=open filled_qty=0 venue_fills={fills_before}",
                observed=f"error: {exc}",
                passed=False,
            )
        )
        ok = False
    if tracked_probe is not None:
        fills_now = len(await broker.fills())
        resting = (
            tracked_probe.status is OrderStatus.OPEN
            and tracked_probe.filled_qty == 0
            and fills_now == fills_before
        )
        report.checks.append(
            CanaryCheck(
                name="cancel_probe.resting",
                expected=f"status=open filled_qty=0 venue_fills={fills_before}",
                observed=(
                    f"status={tracked_probe.status.value} "
                    f"filled_qty={tracked_probe.filled_qty} venue_fills={fills_now}"
                ),
                passed=resting,
            )
        )
        ok = ok and resting

    if ok:
        try:
            cancelled = await router.cancel(report.probe_cid)
            cancel_ok = (
                cancelled.status is OrderStatus.CANCELLED and cancelled.filled_qty == 0
            )
            observed = (
                f"status={cancelled.status.value} filled_qty={cancelled.filled_qty}"
            )
        except _ORDER_ERRORS as exc:
            cancel_ok = False
            observed = f"error: {exc}"
        report.checks.append(
            CanaryCheck(
                name="cancel_probe.cancelled",
                expected="status=cancelled filled_qty=0",
                observed=observed,
                passed=cancel_ok,
            )
        )
        ok = ok and cancel_ok

    if ok:
        balances_now = dict(await broker.balances())
        unmoved = balances_now == report.initial_balances
        report.checks.append(
            CanaryCheck(
                name="cancel_probe.balances_unmoved",
                expected=_fmt_balances(report.initial_balances),
                observed=_fmt_balances(balances_now),
                passed=unmoved,
            )
        )
        ok = ok and unmoved

    if ok:
        store.flush()
        row = store.get_order(report.probe_cid)
        persisted = (
            row is not None
            and row.status is OrderStatus.CANCELLED
            and row.filled_qty == 0
        )
        report.checks.append(
            CanaryCheck(
                name="cancel_probe.cancelled_persisted",
                expected="store status=cancelled filled_qty=0",
                observed=(
                    "store row missing"
                    if row is None
                    else f"store status={row.status.value} filled_qty={row.filled_qty}"
                ),
                passed=persisted,
            )
        )
        ok = ok and persisted

    # --- idempotency probe: a duplicate client-order-id must dedup ------------ #
    if ok:
        open_before = len(await broker.open_orders())
        fills_before = len(await broker.fills())
        store.flush()
        rows_before = len(store.orders())
        expected = (
            f"original order returned venue_open={open_before} "
            f"venue_fills={fills_before} store_orders={rows_before}"
        )
        try:
            # A genuine duplicate submission: a NEW Order object carrying the
            # probe's client-order-id, exactly as a retry would re-build it.
            returned: Order | None = await router.submit(
                Order(
                    client_order_id=report.probe_cid,
                    instrument=instrument,
                    side=OrderSide.BUY,
                    qty=probe_qty,
                    type=OrderType.LIMIT,
                    limit_price=probe_price,
                )
            )
            error: Exception | None = None
        except _ORDER_ERRORS as exc:
            returned, error = None, exc
        open_after = len(await broker.open_orders())
        fills_after = len(await broker.fills())
        store.flush()
        rows_after = len(store.orders())
        deduped = (
            error is None
            and returned is tracked_probe
            and open_after == open_before
            and fills_after == fills_before
            and rows_after == rows_before
        )
        if error is not None:
            observed = f"error: {error}"
        else:
            identity = (
                "original order returned"
                if returned is tracked_probe
                else "a different order returned"
            )
            observed = (
                f"{identity} venue_open={open_after} "
                f"venue_fills={fills_after} store_orders={rows_after}"
            )
        report.checks.append(
            CanaryCheck(
                name="idempotency.deduped",
                expected=expected,
                observed=observed,
                passed=deduped,
            )
        )
        ok = ok and deduped

    # --- round-trip: market buy qty, confirm; market sell qty, confirm; flat -- #
    if ok:
        ok = await _market_leg(engine, report, report.buy_cid, OrderSide.BUY)
    if ok:
        position = engine.tracker.position(instrument)
        net_qty = position.net_qty if position is not None else money("0")
        in_tracker = net_qty == qty
        report.checks.append(
            CanaryCheck(
                name="roundtrip.buy_in_tracker",
                expected=f"net_qty={qty}",
                observed=f"net_qty={net_qty}",
                passed=in_tracker,
            )
        )
        ok = ok and in_tracker
    if ok:
        ok = await _market_leg(engine, report, report.sell_cid, OrderSide.SELL)
    if ok:
        position = engine.tracker.position(instrument)
        net_qty = position.net_qty if position is not None else money("0")
        flat = net_qty == 0
        report.checks.append(
            CanaryCheck(
                name="roundtrip.flat",
                expected="net_qty=0",
                observed=f"net_qty={net_qty}",
                passed=flat,
            )
        )
        ok = ok and flat

    # --- final snapshot + verdict (always, even on an aborted run) ------------ #
    if not ok:
        # Best-effort cleanup: never leave a canary order live on an abort.
        # (A reducing action, not a placement — the stop-placing rule holds.)
        for cid in (report.probe_cid, report.buy_cid, report.sell_cid):
            live = router.get(cid)
            if live is not None and not live.is_terminal:
                try:
                    await router.cancel(cid)
                except _ORDER_ERRORS:
                    pass  # the report already fails; cleanup is best-effort
    store.flush()
    report.final_balances = dict(await broker.balances())
    report.cost = -engine.perf.realised_pnl()
    if ok and oracle is not None:
        report.checks.extend(oracle(engine, report))
    return report


async def _market_leg(
    engine: Engine,
    report: CanaryReport,
    cid: str,
    side: OrderSide,
) -> bool:
    """Place one market leg of the round-trip and confirm it filled + persisted.

    Submits a MARKET order for ``report.qty`` on ``side`` under ``cid``, then
    appends two checks: the tracked order is ``FILLED`` with
    ``filled_qty == qty``, and the **store** row agrees. Returns whether both
    held (the caller stops placing on ``False``). A submission error becomes a
    failed check, never an exception out of the scenario.
    """
    leg = "buy" if side is OrderSide.BUY else "sell"
    tracked: Order | None = None
    try:
        tracked = await engine.router.submit(
            Order(
                client_order_id=cid,
                instrument=report.instrument,
                side=side,
                qty=report.qty,
                type=OrderType.MARKET,
            )
        )
    except _ORDER_ERRORS as exc:
        report.checks.append(
            CanaryCheck(
                name=f"roundtrip.{leg}_filled",
                expected=f"status=filled filled_qty={report.qty}",
                observed=f"error: {exc}",
                passed=False,
            )
        )
        return False
    filled = tracked.status is OrderStatus.FILLED and tracked.filled_qty == report.qty
    report.checks.append(
        CanaryCheck(
            name=f"roundtrip.{leg}_filled",
            expected=f"status=filled filled_qty={report.qty}",
            observed=f"status={tracked.status.value} filled_qty={tracked.filled_qty}",
            passed=filled,
        )
    )
    if not filled:
        return False
    store = engine.store
    assert store is not None  # guarded at run_canary entry
    store.flush()
    row = store.get_order(cid)
    in_store = (
        row is not None
        and row.status is OrderStatus.FILLED
        and row.filled_qty == report.qty
    )
    report.checks.append(
        CanaryCheck(
            name=f"roundtrip.{leg}_in_store",
            expected=f"store status=filled filled_qty={report.qty}",
            observed=(
                "store row missing"
                if row is None
                else f"store status={row.status.value} filled_qty={row.filled_qty}"
            ),
            passed=in_store,
        )
    )
    return in_store
