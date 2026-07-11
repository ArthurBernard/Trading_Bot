"""Tests for :mod:`trading_bot.application.order_prep` — the pure leg policy.

Table-driven over exact :class:`~decimal.Decimal` values (never float), one
group per pinned-policy clause (``doc/dev/plans/venue-minimums/00-plan.md``):

* at/above the binding minimum → submit, lot-quantized, otherwise unchanged;
* within ``min_order_ratio`` of the minimum → round **up** to it exactly —
  both the ``min_qty``-bound and the ``min_notional``-bound cases, including
  an off-grid ``min_notional/price`` quotient snapped UP to the lot grid;
* below the threshold → skip, with the binding minimum named in the reason;
* lot quantization happens FIRST (a delta that quantizes to zero → skip);
* the spot-sell cap trio: capped below the minimum → skip; capped at/above
  the minimum → submit the capped quantity; a short-extending sell (signed
  ``position_qty <= 0``) is never capped;
* no spec at all → permissive passthrough of the lot-quantized quantity;
* ratio bounds: ``1`` never rounds up; a tiny ratio almost always does.
"""

from __future__ import annotations

# Built-in
from decimal import Decimal

# Third-party
import pytest

# Local
from trading_bot.application.order_prep import prepare_leg
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money

BTC_USDT = Symbol("BTC", "USDT")

#: min_qty binds (no notional): lot 0.0001, min_qty 0.001.
QTY_BOUND = Instrument(BTC_USDT, qty_precision=4, min_qty=money("0.001"))
#: min_notional binds: min 5 USDT at price 10000 → 0.0005 > min_qty 0.0001.
NOTIONAL_BOUND = Instrument(
    BTC_USDT,
    qty_precision=4,
    min_qty=money("0.0001"),
    min_notional=money("5"),
)
#: No spec at all — the permissive passthrough.
BARE = Instrument(BTC_USDT)

HALF = money("0.5")


def _leg(
    delta: str,
    instrument: Instrument = QTY_BOUND,
    *,
    position: str = "0",
    ratio: str = "0.5",
    price: str | None = "10000",
):
    """Drive :func:`prepare_leg` with string-exact Decimals."""
    return prepare_leg(
        money(delta),
        instrument,
        position_qty=money(position),
        min_order_ratio=money(ratio),
        price=None if price is None else money(price),
    )


# --- at/above the minimum: submit (quantized, otherwise unchanged) ---------- #


def test_above_min_submits_unchanged() -> None:
    """A leg at/above the binding minimum submits its lot-quantized quantity."""
    decision = _leg("0.0050")
    assert decision.action == "submit"
    assert decision.qty == Decimal("0.0050")


def test_submit_lot_quantizes_down_first() -> None:
    """Quantization precedes the comparison: an off-grid delta is snapped down."""
    decision = _leg("0.00509")  # off the 0.0001 grid
    assert decision.action == "submit"
    assert decision.qty == Decimal("0.0050")


def test_exactly_at_min_submits() -> None:
    """The boundary ``qty_q == min`` belongs to submit, not round_up."""
    decision = _leg("0.001")
    assert decision.action == "submit"
    assert decision.qty == Decimal("0.0010")


# --- round-up band: threshold*min <= qty < min ------------------------------ #


def test_round_up_min_qty_bound() -> None:
    """Between 0.5x and 1x of a binding min_qty the leg is bumped to it exactly."""
    decision = _leg("0.0006")
    assert decision.action == "round_up"
    assert decision.qty == Decimal("0.0010")
    assert "min_qty 0.001" in decision.reason
    assert "0.0006" in decision.reason


def test_round_up_min_notional_bound() -> None:
    """The notional minimum (min_notional/price) binds when larger than min_qty."""
    # min = 5 / 10000 = 0.0005 (on the 0.0001 grid); threshold = 0.00025.
    decision = _leg("0.0003", NOTIONAL_BOUND)
    assert decision.action == "round_up"
    assert decision.qty == Decimal("0.0005")
    assert "min_notional 5" in decision.reason
    assert "10000" in decision.reason


def test_round_up_off_grid_notional_min_snaps_up_to_lot() -> None:
    """An off-grid min_notional/price quotient is snapped UP to the lot grid.

    5 / 6000 = 0.000833... is not on the 0.0001 grid; the bump target must be
    the next grid step (0.0009) so the submitted qty clears both the lot and
    the notional axis (0.0009 * 6000 = 5.4 >= 5).
    """
    decision = _leg("0.0005", NOTIONAL_BOUND, price="6000")
    assert decision.action == "round_up"
    assert decision.qty == Decimal("0.0009")
    assert decision.qty * money("6000") >= money("5")


def test_exactly_at_threshold_rounds_up() -> None:
    """The lower boundary ``qty_q == ratio*min`` belongs to round_up."""
    decision = _leg("0.0005")  # exactly 0.5 * 0.001
    assert decision.action == "round_up"
    assert decision.qty == Decimal("0.0010")


# --- below the threshold: skip ---------------------------------------------- #


def test_below_threshold_skips_naming_the_minimum() -> None:
    """Below ratio*min the leg is skipped and the reason names the minimum."""
    decision = _leg("0.0004")
    assert decision.action == "skip"
    assert "min_qty 0.001" in decision.reason
    assert "0.0004" in decision.reason


def test_notional_dust_skips() -> None:
    """The audit's dust shape: a ~0.5-USDT leg against a 5-USDT notional min."""
    # 0.0001 BTC at 10000 = 1 USDT — 20% of the 5-USDT minimum: below 50%.
    decision = _leg("0.0001", NOTIONAL_BOUND)
    assert decision.action == "skip"
    assert "min_notional 5" in decision.reason


# --- lot quantization first: quantize-to-zero skip --------------------------- #


def test_delta_quantizing_to_zero_skips() -> None:
    """A sub-lot delta cannot be expressed on the venue grid at all → skip."""
    decision = _leg("0.00004")  # below one 0.0001 lot step
    assert decision.action == "skip"
    assert "quantizes to zero" in decision.reason
    assert "0.0001" in decision.reason


# --- the spot-sell cap trio -------------------------------------------------- #


def test_sell_capped_below_min_skips() -> None:
    """Sell 0.6x min holding 0.4x min: the cap (0.0004) is below min → skip."""
    decision = _leg("-0.0006", position="0.0004")
    assert decision.action == "skip"
    assert "capped" in decision.reason
    assert "0.0004" in decision.reason
    assert "min_qty 0.001" in decision.reason


def test_sell_capped_at_held_submits_capped() -> None:
    """Sell 1.5x min holding 1.2x min: capped submit at exactly the held qty."""
    decision = _leg("-0.0015", position="0.0012")
    assert decision.action == "submit"
    assert decision.qty == Decimal("0.0012")
    assert "capped" in decision.reason


def test_short_extending_sell_is_not_capped() -> None:
    """A sell against a flat/short book (signed position <= 0) is never capped."""
    for position in ("0", "-0.0002"):
        decision = _leg("-0.0015", position=position)
        assert decision.action == "submit"
        assert decision.qty == Decimal("0.0015")
        assert "capped" not in decision.reason


def test_sell_within_held_position_is_not_capped() -> None:
    """A long-reducing sell no larger than the held qty passes through uncapped."""
    decision = _leg("-0.0015", position="0.0020")
    assert decision.action == "submit"
    assert decision.qty == Decimal("0.0015")
    assert "capped" not in decision.reason


def test_round_up_beyond_held_position_skips() -> None:
    """A bump that would oversell the held long is skipped, never submitted.

    qty 0.0006 rounds up to the 0.001 minimum, but only 0.0007 is held: the
    cap (0.0007) re-checks against the minimum and fails → skip.
    """
    decision = _leg("-0.0006", position="0.0007")
    assert decision.action == "skip"
    assert "0.0007" in decision.reason


# --- no spec: permissive passthrough ----------------------------------------- #


def test_no_spec_passthrough_submits_verbatim() -> None:
    """A bare instrument (no minimums, no lot) submits the |delta| unchanged."""
    decision = _leg("0.0000079", BARE)
    assert decision.action == "submit"
    assert decision.qty == Decimal("0.0000079")


def test_lot_only_spec_quantizes_but_never_blocks() -> None:
    """A lot without minimums still quantizes; no minimum means no skip band."""
    lot_only = Instrument(BTC_USDT, qty_precision=4)
    decision = _leg("0.00068", lot_only)
    assert decision.action == "submit"
    assert decision.qty == Decimal("0.0006")


def test_min_notional_without_price_is_ignored() -> None:
    """min_notional cannot bind without a reference price (market-order shape)."""
    notional_only = Instrument(BTC_USDT, qty_precision=4, min_notional=money("5"))
    decision = _leg("0.0001", notional_only, price=None)
    assert decision.action == "submit"
    assert decision.qty == Decimal("0.0001")


# --- ratio bounds ------------------------------------------------------------ #


def test_ratio_one_never_rounds_up() -> None:
    """At ratio 1 the round-up band is empty: any sub-minimum leg skips."""
    decision = _leg("0.0009", ratio="1")
    assert decision.action == "skip"


def test_tiny_ratio_almost_always_rounds_up() -> None:
    """A tiny ratio rounds up from far below the minimum (but not from zero)."""
    decision = _leg("0.0001", ratio="0.01")  # 10% of min, >= 1% threshold
    assert decision.action == "round_up"
    assert decision.qty == Decimal("0.0010")


@pytest.mark.parametrize("ratio", ["0", "-0.5", "1.5"])
def test_ratio_outside_unit_interval_raises(ratio: str) -> None:
    """min_order_ratio outside (0, 1] is a config error, rejected loudly."""
    with pytest.raises(ValueError):
        _leg("0.0006", ratio=ratio)
