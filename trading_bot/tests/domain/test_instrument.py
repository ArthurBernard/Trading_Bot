"""Tests for venue-neutral Symbol/Instrument and Kraken normalisation."""

from __future__ import annotations

import pytest

from trading_bot.domain.errors import OrderTooSmall
from trading_bot.domain.instrument import (
    Instrument,
    Symbol,
    normalise,
    parse_binance_symbol,
    parse_kraken_pair,
)
from trading_bot.domain.money import money

# Real Kraken pair strings (legacy X/Z-prefixed form) and their canonical map.
REAL_KRAKEN_PAIRS = [
    ("XXBTZUSD", "BTC", "USD"),
    ("XETHZEUR", "ETH", "EUR"),
    ("XXBTZEUR", "BTC", "EUR"),
    ("XETHZUSD", "ETH", "USD"),
    ("XLTCZUSD", "LTC", "USD"),
    ("XXRPZUSD", "XRP", "USD"),
    ("XETHXXBT", "ETH", "BTC"),
]

# Real Kraken **altname** pair strings (modern, no X/Z-prefix on the base) and
# their true venue meaning. The X/Z-prefixed altnames (XTZ = Tezos, XLM, XMR,
# XRP) are the regression guard for D-3: a naive "trailing ZUSD is legacy fiat"
# split turns ``XTZUSD`` into the wrong market ``XT/USD``.
REAL_KRAKEN_ALTNAME_PAIRS = [
    ("XTZUSD", "XTZ", "USD"),  # Tezos — the D-3 wrong-market bug
    ("XTZEUR", "XTZ", "EUR"),
    ("XTZXBT", "XTZ", "BTC"),
    ("XLMUSD", "XLM", "USD"),
    ("XMRUSD", "XMR", "USD"),
    ("XRPUSD", "XRP", "USD"),
    ("XBTUSD", "BTC", "USD"),
    ("ETHUSD", "ETH", "USD"),
    ("ETHUSDT", "ETH", "USDT"),
    ("ETHXBT", "ETH", "BTC"),
    ("ADAUSD", "ADA", "USD"),
    ("ADAUSDT", "ADA", "USDT"),
    ("DOTUSD", "DOT", "USD"),
    ("SOLUSD", "SOL", "USD"),
    ("LINKUSD", "LINK", "USD"),
    ("ALGOUSD", "ALGO", "USD"),
    ("MATICUSD", "MATIC", "USD"),
    ("DOGEUSD", "DOGE", "USD"),
    ("DOTXBT", "DOT", "BTC"),
]


class TestNormalise:
    def test_xbt_alias(self) -> None:
        assert normalise("XBT") == "BTC"
        assert normalise("XXBT") == "BTC"

    def test_xdg_alias(self) -> None:
        assert normalise("XDG") == "DOGE"
        assert normalise("XXDG") == "DOGE"

    def test_fiat_z_prefix_stripped(self) -> None:
        assert normalise("ZUSD") == "USD"
        assert normalise("ZEUR") == "EUR"
        assert normalise("ZGBP") == "GBP"

    def test_crypto_x_prefix_stripped(self) -> None:
        assert normalise("XETH") == "ETH"
        assert normalise("XLTC") == "LTC"
        assert normalise("XXRP") == "XRP"

    def test_canonical_passthrough(self) -> None:
        assert normalise("BTC") == "BTC"
        assert normalise("ETH") == "ETH"
        assert normalise("USDT") == "USDT"

    def test_case_and_whitespace_insensitive(self) -> None:
        assert normalise("  xxbt ") == "BTC"
        assert normalise("zusd") == "USD"

    # --- D-10: only genuine legacy X/Z codes get their prefix stripped ------ #

    def test_tezos_xtz_is_not_stripped(self) -> None:
        # XTZ is Tezos, not an X-prefixed "TZ" — it must pass through intact.
        assert normalise("XTZ") == "XTZ"

    def test_non_legacy_four_char_x_code_is_left_intact(self) -> None:
        # A 4-char code starting with X that is NOT a genuine Kraken legacy code
        # must not have its leading X stripped (the D-10 over-eager rule bug).
        assert normalise("XRPX") == "XRPX"
        assert normalise("XYZW") == "XYZW"

    def test_non_legacy_four_char_z_code_is_left_intact(self) -> None:
        # ZABC is not a Z-fiat legacy code (ABC is not a known fiat).
        assert normalise("ZABC") == "ZABC"

    def test_tezos_legacy_xxtz_is_stripped(self) -> None:
        # The genuine legacy form XXTZ does collapse to XTZ.
        assert normalise("XXTZ") == "XTZ"


class TestParseKrakenPair:
    @pytest.mark.parametrize("pair,base,quote", REAL_KRAKEN_PAIRS)
    def test_real_legacy_pairs(self, pair: str, base: str, quote: str) -> None:
        sym = parse_kraken_pair(pair)
        assert sym == Symbol(base, quote)
        assert str(sym) == f"{base}/{quote}"

    def test_altname_form(self) -> None:
        assert parse_kraken_pair("ETHUSD") == Symbol("ETH", "USD")
        assert parse_kraken_pair("XBTUSD") == Symbol("BTC", "USD")
        assert parse_kraken_pair("ETHXBT") == Symbol("ETH", "BTC")

    @pytest.mark.parametrize("pair,base,quote", REAL_KRAKEN_ALTNAME_PAIRS)
    def test_real_altname_pairs(self, pair: str, base: str, quote: str) -> None:
        # Verification on real Kraken altnames: the split must match the venue's
        # true base/quote, including the X/Z-prefixed altnames (XTZ, XLM, XMR).
        sym = parse_kraken_pair(pair)
        assert sym == Symbol(base, quote)
        assert str(sym) == f"{base}/{quote}"

    def test_xtz_altname_is_tezos_not_xt(self) -> None:
        # D-3 regression: XTZUSD is Tezos (XTZ/USD), never the wrong XT/USD.
        sym = parse_kraken_pair("XTZUSD")
        assert sym == Symbol("XTZ", "USD")
        assert sym.base == "XTZ"
        assert sym.quote == "USD"

    def test_explicit_separator(self) -> None:
        assert parse_kraken_pair("BTC/USD") == Symbol("BTC", "USD")
        assert parse_kraken_pair("XBT-USD") == Symbol("BTC", "USD")

    def test_unparseable_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_kraken_pair("ZZ")


# Representative real Binance pair strings (canonical codes, no separator).
REAL_BINANCE_PAIRS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "ETHBTC",
]


class TestParseBinanceSymbol:
    def test_concatenated_form(self) -> None:
        assert parse_binance_symbol("BTCUSDT") == Symbol("BTC", "USDT")
        assert parse_binance_symbol("ETHBTC") == Symbol("ETH", "BTC")
        assert parse_binance_symbol("BNBUSDT") == Symbol("BNB", "USDT")

    def test_longest_quote_wins(self) -> None:
        # FDUSD must win over the shorter USD suffix.
        assert parse_binance_symbol("ETHFDUSD") == Symbol("ETH", "FDUSD")

    @pytest.mark.parametrize("base", ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE"])
    def test_round_trip_from_symbol(self, base: str) -> None:
        sym = Symbol(base, "USDT")
        rendered = sym.to_venue_symbol("binance")
        assert parse_binance_symbol(rendered) == sym

    def test_explicit_separator(self) -> None:
        assert parse_binance_symbol("BTC/USDT") == Symbol("BTC", "USDT")
        assert parse_binance_symbol("BTC-USDT") == Symbol("BTC", "USDT")
        assert parse_binance_symbol("btc_usdt") == Symbol("BTC", "USDT")

    def test_no_xbt_aliasing_on_render(self) -> None:
        # Unlike Kraken, Binance never aliases BTC to XBT.
        assert Symbol("BTC", "USDT").to_venue_symbol("binance") == "BTCUSDT"
        assert Symbol("ETH", "BTC").to_venue_symbol("binance") == "ETHBTC"

    def test_unparseable_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_binance_symbol("BTC")  # bare base, no quote suffix left
        with pytest.raises(ValueError):
            parse_binance_symbol("ZZZZ")  # no known quote suffix

    @pytest.mark.parametrize("pair", REAL_BINANCE_PAIRS)
    def test_real_pairs_round_trip(self, pair: str) -> None:
        # Honesty check: the parse table matches the codes Binance actually uses.
        assert parse_binance_symbol(pair).to_venue_symbol("binance") == pair


class TestSymbol:
    def test_str_is_base_slash_quote(self) -> None:
        assert str(Symbol("BTC", "USD")) == "BTC/USD"

    def test_canonicalises_legs(self) -> None:
        assert Symbol("XBT", "ZUSD") == Symbol("BTC", "USD")
        assert Symbol("xxbt", "zeur") == Symbol("BTC", "EUR")

    def test_frozen_immutable(self) -> None:
        sym = Symbol("BTC", "USD")
        with pytest.raises(Exception):
            sym.base = "ETH"  # type: ignore[misc]

    def test_hashable_and_equal(self) -> None:
        a = Symbol("BTC", "USD")
        b = Symbol("xbt", "zusd")
        assert a == b
        assert hash(a) == hash(b)
        assert {a, b} == {a}

    def test_to_venue_symbol_kraken(self) -> None:
        assert Symbol("BTC", "USD").to_venue_symbol("kraken") == "XBTUSD"
        assert Symbol("ETH", "BTC").to_venue_symbol("kraken") == "ETHXBT"
        assert Symbol("DOGE", "USD").to_venue_symbol("kraken") == "XDGUSD"

    def test_to_venue_symbol_generic(self) -> None:
        assert Symbol("BTC", "USDT").to_venue_symbol("binance") == "BTCUSDT"

    def test_to_venue_symbol_round_trip(self) -> None:
        # Render to Kraken altname then parse back to the same canonical symbol.
        for _, base, quote in REAL_KRAKEN_PAIRS:
            sym = Symbol(base, quote)
            rendered = sym.to_venue_symbol("kraken")
            assert parse_kraken_pair(rendered) == sym


class TestInstrument:
    def test_carries_symbol_and_precisions(self) -> None:
        inst = Instrument(Symbol("BTC", "USD"), price_precision=1, qty_precision=8)
        assert inst.symbol == Symbol("BTC", "USD")
        assert inst.price_precision == 1
        assert inst.qty_precision == 8
        assert str(inst) == "BTC/USD"

    def test_precisions_optional(self) -> None:
        inst = Instrument(Symbol("ETH", "EUR"))
        assert inst.price_precision is None
        assert inst.qty_precision is None

    def test_frozen_and_hashable(self) -> None:
        inst = Instrument(Symbol("BTC", "USD"), price_precision=1)
        assert isinstance(hash(inst), int)
        with pytest.raises(Exception):
            inst.price_precision = 2  # type: ignore[misc]

    def test_minimums_optional_and_default_none(self) -> None:
        inst = Instrument(Symbol("BTC", "USD"))
        assert inst.min_qty is None
        assert inst.min_notional is None


class TestInstrumentQuantization:
    """The venue tick/lot quantization + sub-minimum rejection (B-2)."""

    BTC_USD = Instrument(Symbol("BTC", "USD"), price_precision=1, qty_precision=8)

    def test_price_and_qty_step_from_precision(self) -> None:
        assert self.BTC_USD.price_step == money("0.1")
        assert self.BTC_USD.qty_step == money("0.00000001")

    def test_step_none_when_precision_unknown(self) -> None:
        inst = Instrument(Symbol("BTC", "USD"))
        assert inst.price_step is None
        assert inst.qty_step is None

    def test_quantize_qty_rounds_down(self) -> None:
        # An over-precise qty snaps to the lot step, rounding *down* (never sells
        # more than held): 0.123456789 -> 0.12345678 at 8 dp.
        inst = self.BTC_USD
        assert inst.quantize_qty(money("0.123456789")) == money("0.12345678")

    def test_quantize_price_rounds_down(self) -> None:
        inst = self.BTC_USD
        assert inst.quantize_price(money("27123.456789")) == money("27123.4")

    def test_quantize_passthrough_when_precision_unknown(self) -> None:
        inst = Instrument(Symbol("BTC", "USD"))
        assert inst.quantize_qty(money("0.123456789")) == money("0.123456789")
        assert inst.quantize_price(money("27123.456789")) == money("27123.456789")

    def test_prepare_over_precise_snaps_to_step(self) -> None:
        qty, limit, stop = self.BTC_USD.prepare_order_values(
            money("0.123456789"), limit_price=money("27123.456789")
        )
        assert qty == money("0.12345678")
        assert limit == money("27123.4")
        assert stop is None

    def test_prepare_sub_lot_rounds_to_zero_rejected(self) -> None:
        # A qty below the lot step quantizes to zero -> doomed order, rejected.
        with pytest.raises(OrderTooSmall, match="rounds to zero"):
            self.BTC_USD.prepare_order_values(money("0.000000001"))

    def test_prepare_below_min_qty_rejected(self) -> None:
        inst = Instrument(
            Symbol("BTC", "USD"),
            price_precision=1,
            qty_precision=8,
            min_qty=money("0.0001"),
        )
        with pytest.raises(OrderTooSmall, match="below the minimum"):
            inst.prepare_order_values(money("0.00005"))

    def test_prepare_below_min_notional_rejected(self) -> None:
        inst = Instrument(
            Symbol("BTC", "USD"),
            price_precision=1,
            qty_precision=8,
            min_notional=money("10"),
        )
        # 0.0001 BTC * 30000 = 3 USD < 10 USD minimum notional.
        with pytest.raises(OrderTooSmall, match="below the minimum"):
            inst.prepare_order_values(
                money("0.0001"), limit_price=money("30000")
            )

    def test_prepare_meets_min_notional_passes(self) -> None:
        inst = Instrument(
            Symbol("BTC", "USD"),
            price_precision=1,
            qty_precision=8,
            min_notional=money("10"),
        )
        qty, limit, _ = inst.prepare_order_values(
            money("0.001"), limit_price=money("30000")
        )
        assert qty == money("0.00100000")
        assert limit == money("30000.0")

    def test_prepare_market_order_skips_notional_check(self) -> None:
        # No price to compute notional client-side: the notional check is skipped.
        inst = Instrument(
            Symbol("BTC", "USD"),
            price_precision=1,
            qty_precision=8,
            min_notional=money("10"),
        )
        qty, limit, stop = inst.prepare_order_values(money("0.001"))
        assert qty == money("0.00100000")
        assert limit is None and stop is None
