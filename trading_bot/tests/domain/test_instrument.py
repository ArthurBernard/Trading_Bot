"""Tests for venue-neutral Symbol/Instrument and Kraken normalisation."""

from __future__ import annotations

import pytest

from trading_bot.domain.instrument import (
    Instrument,
    Symbol,
    normalise,
    parse_binance_symbol,
    parse_kraken_pair,
)

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
