"""The order-preparation policy — venue minimums enforced *upstream*, per leg.

The pinned policy of ``doc/dev/plans/venue-minimums/00-plan.md``: a rebalance
leg whose quantity would be rejected by the venue (below its lot / ``min_qty``
/ ``min_notional``) is handled **before** an order is ever built — rounded UP
to the minimum when it is close (``min_order_ratio`` of the minimum or more),
SKIPPED (no submit, one log line, the residual recomputes naturally next
rebalance) when it is far below — rather than submitted to be rejected (live)
or, worse, filled by a permissive simulator (the 2026-07-11 audit's
0.0000079-BTC ≈ 0.50-USDT dust buy). :func:`prepare_leg` is that policy as a
pure function; the :class:`~trading_bot.application.portfolio_runner.
PortfolioRunner` applies it to every leg using the venue spec resolved by the
:class:`~trading_bot.application.instrument_specs.InstrumentSpecResolver`.

The policy, verbatim (per leg, with a resolved spec)
----------------------------------------------------
``min`` is the *binding* minimum — the larger of ``min_qty`` and
``min_notional / price`` (each participating only when known) — and ``qty_q``
is ``|delta|`` lot-quantized FIRST (:meth:`~trading_bot.domain.instrument.
Instrument.quantize_qty`, round-down):

* ``qty_q >= min`` → **submit** ``qty_q`` (as today, quantized);
* ``min_order_ratio × min <= qty_q < min`` → **round up** to the minimum
  (snapped *up* to the lot grid, so the bumped quantity is venue-acceptable on
  both the lot and the notional axis);
* ``qty_q < min_order_ratio × min`` → **skip** — no submit, no reject noise;
* **spot-sell cap** — a sell that reduces a long position (signed
  ``position_qty > 0``) is capped at the held quantity (lot-quantized down, so
  a sell never sells more than held); a capped quantity below the minimum →
  **skip** (never oversell into a venue reject or an unintended short). A sell
  that opens/extends a short (``position_qty <= 0``) is NOT capped — the cap
  protects spot longs only;
* **no spec** (no known minimum at all) → permissive **submit** of the
  lot-quantized quantity — today's behaviour, unchanged: the policy degrades,
  it never blocks trading;
* a delta that lot-quantizes to **zero** → **skip** (sub-lot dust cannot be
  expressed on the venue's grid at all).

Money discipline
----------------
Everything is exact :class:`~decimal.Decimal` end to end; the one division
(``min_notional / price``) goes through the codebase's exact-money pattern
(``money(str(...))``), mirroring
:func:`~trading_bot.application.portfolio.weights_to_signals`. The policy only
shapes **order quantities** — it never touches positions or fills (fills stay
the sole PnL truth).

Scope — a follow-up seam
------------------------
Only the :class:`~trading_bot.application.portfolio_runner.PortfolioRunner`
applies this policy today (the dashboard strategies are portfolio units). The
single-instrument :class:`~trading_bot.application.strategy_runner.
StrategyRunner` is deliberately OUT of scope for this leaf and still submits
its deltas unprepared — wiring :func:`prepare_leg` into its order build is the
follow-up seam (the function is runner-agnostic by construction: pure, domain
imports only).
"""

from __future__ import annotations

# Built-in
from dataclasses import dataclass
from decimal import ROUND_UP
from typing import Literal

# Local
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money, money, quantize

__all__ = ["LegDecision", "prepare_leg"]


@dataclass(frozen=True, slots=True)
class LegDecision:
    """One leg's order-preparation outcome — what to do, at what quantity, and why.

    Attributes
    ----------
    action : {"submit", "round_up", "skip"}
        ``"submit"`` — route ``qty`` (the lot-quantized, possibly sell-capped
        quantity); ``"round_up"`` — route ``qty`` (bumped up to the venue
        minimum); ``"skip"`` — route nothing (log ``reason``; the residual is
        recomputed by the next rebalance).
    qty : Decimal
        The final **absolute** base-unit quantity to submit. Meaningless for
        ``"skip"`` (carried for diagnostics only — never route it).
    reason : str
        One human sentence for the log, carrying the exact Decimals involved
        and naming the minimum that bound (``min_qty`` / ``min_notional``).

    """

    action: Literal["submit", "round_up", "skip"]
    qty: Money
    reason: str


def prepare_leg(
    delta: Money,
    instrument: Instrument,
    *,
    position_qty: Money,
    min_order_ratio: Money,
    price: Money | None,
) -> LegDecision:
    """Apply the venue-minimum policy to one rebalance leg (pure, no I/O).

    See the module docstring for the pinned policy this implements verbatim:
    lot-quantize first, compare against the binding minimum (the larger of
    ``min_qty`` and ``min_notional / price``), round up when within
    ``min_order_ratio`` of it, skip otherwise, and cap a long-reducing sell at
    the held quantity (skipping when the cap itself is below the minimum).

    Parameters
    ----------
    delta : Decimal
        The leg's **signed** target change in base units (``> 0`` buy, ``< 0``
        sell) — the runner's ``signal.delta_to(position)``. Must be non-zero
        (an on-target leg never reaches order preparation).
    instrument : Instrument
        The venue-resolved instrument spec (``min_qty`` / ``min_notional`` /
        ``qty_precision``). A bare instrument (no spec) yields the permissive
        passthrough.
    position_qty : Decimal
        The **signed** current net position in base units (flat = ``0``). Its
        sign decides the spot-sell cap: a sell with ``position_qty > 0``
        reduces a long and is capped at it; a sell with ``position_qty <= 0``
        extends a short and is not.
    min_order_ratio : Decimal
        The round-up threshold as a fraction of the binding minimum, in
        ``(0, 1]``. At ``1`` the round-up band is empty (a sub-minimum leg is
        always skipped); smaller values round up more aggressively.
    price : Decimal or None
        The leg's reference price (the same latest close the sizing used),
        needed to express ``min_notional`` in base units. ``None`` (or a
        non-positive value) leaves ``min_notional`` out of the binding minimum.

    Returns
    -------
    LegDecision
        The action, the final absolute quantity and the human-readable reason.

    Raises
    ------
    ValueError
        If ``min_order_ratio`` is outside ``(0, 1]``.

    """
    if not (0 < min_order_ratio <= 1):
        raise ValueError(f"min_order_ratio must be in (0, 1], got {min_order_ratio}")

    # 1. Lot-quantize FIRST (round-down, the venue-safe direction): every
    #    comparison below is against a quantity the venue's grid can express.
    qty_q = instrument.quantize_qty(abs(delta))
    if qty_q <= 0:
        return LegDecision(
            action="skip",
            qty=qty_q,
            reason=(
                f"|delta| {abs(delta)} quantizes to zero at lot step "
                f"{instrument.qty_step}"
            ),
        )

    # 2. The binding minimum: the larger of min_qty and min_notional/price,
    #    each participating only when known. The division goes through the
    #    exact-money pattern (money(str(...))) — never float.
    minimums: list[tuple[str, Money]] = []
    if instrument.min_qty is not None:
        minimums.append((f"min_qty {instrument.min_qty}", instrument.min_qty))
    if instrument.min_notional is not None and price is not None and price > 0:
        min_from_notional = money(str(instrument.min_notional / price))
        minimums.append(
            (
                f"min_notional {instrument.min_notional} at price {price}",
                min_from_notional,
            )
        )

    if not minimums:
        # No spec at all: permissive passthrough of the lot-quantized quantity
        # (today's behaviour) — the policy degrades, it never blocks trading.
        return LegDecision(
            action="submit",
            qty=qty_q,
            reason=f"no venue minimum known for {instrument}; submitting {qty_q}",
        )

    bound_name, binding_min = max(minimums, key=lambda item: item[1])

    # 3. The threshold rule: submit at/above the minimum, round up within
    #    min_order_ratio of it, skip below the threshold.
    threshold = min_order_ratio * binding_min
    if qty_q >= binding_min:
        action: Literal["submit", "round_up"] = "submit"
        final = qty_q
        reason = f"qty {qty_q} meets the venue minimum {binding_min} ({bound_name})"
    elif qty_q >= threshold:
        action = "round_up"
        # Snap the minimum UP to the lot grid so the bumped quantity clears the
        # minimum on both the lot axis and (via qty >= min_notional/price) the
        # notional axis.
        final = _ceil_to_lot(binding_min, instrument)
        reason = (
            f"qty {qty_q} is below the venue minimum {binding_min} "
            f"({bound_name}) but within {min_order_ratio} of it; "
            f"rounding up to {final}"
        )
    else:
        return LegDecision(
            action="skip",
            qty=qty_q,
            reason=(
                f"qty {qty_q} is below {min_order_ratio} x the venue minimum "
                f"{binding_min} ({bound_name}); skipping — the next rebalance "
                f"recomputes the residual"
            ),
        )

    # 4. Spot-sell cap: a sell reducing a long is capped at the held quantity
    #    (lot-quantized DOWN — a sell never sells more than held). The signed
    #    position decides: position_qty <= 0 means the sell opens/extends a
    #    short and is deliberately NOT capped.
    if delta < 0 and position_qty > 0:
        cap = instrument.quantize_qty(position_qty)
        if final > cap:
            # `cap <= 0` (a held position below one lot step) is skipped even if
            # a pathological venue published a zero minimum: a zero-quantity
            # order must never be built.
            if cap < binding_min or cap <= 0:
                return LegDecision(
                    action="skip",
                    qty=qty_q,
                    reason=(
                        f"sell of {final} capped at held position {cap}, which "
                        f"is below the venue minimum {binding_min} "
                        f"({bound_name}); skipping"
                    ),
                )
            return LegDecision(
                action="submit",
                qty=cap,
                reason=(
                    f"sell of {final} capped at held position {cap} "
                    f"(venue minimum {binding_min}, {bound_name})"
                ),
            )

    return LegDecision(action=action, qty=final, reason=reason)


def _ceil_to_lot(value: Money, instrument: Instrument) -> Money:
    """Snap ``value`` UP to ``instrument``'s lot grid (identity when unknown).

    The round-up direction is deliberate and used only for the bump target: a
    quantity rounded up from the binding minimum still clears that minimum,
    whereas rounding it down could land one lot step below it.
    """
    step = instrument.qty_step
    if step is None:
        return value
    return quantize(value, step, rounding=ROUND_UP)
