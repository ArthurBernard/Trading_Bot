"""Venue-neutral :class:`Symbol` / :class:`Instrument` + Kraken normalisation.

The domain speaks in *canonical* assets (``BTC``, ``ETH``, ``USD``, ``DOGE``)
and canonical pairs (``BTC/USD``). Venues use their own codes; Kraken in
particular has two layers of weirdness:

* an ``X`` prefix on crypto asset codes and a ``Z`` prefix on fiat codes in its
  *legacy* 4-character form — ``XXBT``, ``XETH``, ``ZUSD``, ``ZEUR`` — so that a
  legacy pair string looks like ``XXBTZUSD`` or ``XETHZEUR``;
* two ticker aliases that differ from the rest of the world: ``XBT`` for Bitcoin
  and ``XDG`` for Dogecoin.

:func:`normalise` collapses any of those venue codes to the canonical asset.
:func:`parse_kraken_pair` turns a Kraken pair string into a :class:`Symbol`, and
:meth:`Symbol.to_venue_symbol` renders a canonical symbol back to a venue's code.

Binance is simpler: it concatenates canonical asset codes with no separator
(``BTCUSDT``, ``ETHBTC``) and uses no X/Z prefixes and no ``XBT`` alias.
:func:`parse_binance_symbol` is the inverse that rebuilds a :class:`Symbol` from
a Binance pair string.

The alias table (``XBT→BTC``, ``XDG→DOGE``) is the one used by the **dccd**
Kraken adapter (``dccd/sources/kraken.py`` ``_KRAKEN_ALIASES`` and
``dccd/domain/symbol.py`` ``_ALIASES``); the X/Z legacy-prefix rule is Kraken's
documented asset-naming scheme. This module is pure: no I/O, no network — it
never calls Kraken's ``/Assets`` or ``/AssetPairs`` endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Symbol",
    "Instrument",
    "normalise",
    "parse_kraken_pair",
    "parse_binance_symbol",
]

# --- Kraken asset normalisation -------------------------------------------- #

# Ticker aliases: venue code -> canonical code. Mined from the dccd Kraken
# adapter (BTC<->XBT, DOGE<->XDG). Keys are the *stripped* codes (no X/Z
# prefix), because we strip the legacy prefix first.
_KRAKEN_TO_CANONICAL: dict[str, str] = {
    "XBT": "BTC",
    "XDG": "DOGE",
}
# Inverse, for rendering a canonical asset back to Kraken's altname.
_CANONICAL_TO_KRAKEN: dict[str, str] = {
    canon: venue for venue, canon in _KRAKEN_TO_CANONICAL.items()
}

# Known fiat codes (canonical, 3-char). Kraken prefixes these with ``Z`` in its
# legacy 4-char form. Used to split a concatenated legacy pair on the boundary.
_FIAT: frozenset[str] = frozenset(
    {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF"}
)

# Genuine Kraken *legacy* 4-char asset codes (the ``X``-prefixed crypto and
# ``Z``-prefixed fiat forms), listed explicitly so :func:`normalise` strips the
# prefix ONLY for these — never for a modern altname that merely happens to be
# 4 chars long and start with ``X`` (e.g. ``XTZ`` is Tezos, not an X-prefixed
# ``TZ``). The crypto set is Kraken's documented legacy roster; the fiat set is
# ``{"Z"+f for f in _FIAT}``. A code in neither set passes through unchanged.
_KRAKEN_LEGACY_X: frozenset[str] = frozenset(
    {
        "XXBT",  # Bitcoin (-> XBT -> BTC)
        "XETH",  # Ether
        "XXRP",  # Ripple
        "XXLM",  # Stellar Lumens
        "XXMR",  # Monero
        "XXDG",  # Dogecoin (-> XDG -> DOGE)
        "XLTC",  # Litecoin
        "XETC",  # Ethereum Classic
        "XREP",  # Augur
        "XMLN",  # Enzyme (Melon)
        "XZEC",  # Zcash
        "XXTZ",  # Tezos (legacy X-prefixed form of XTZ)
        "XICN",  # Iconomi
        "XNMC",  # Namecoin
        "XXVN",  # Vanacoin
    }
)
_KRAKEN_LEGACY_Z: frozenset[str] = frozenset(f"Z{f}" for f in _FIAT)

# Quote codes that appear at the *end* of a modern Kraken **altname** pair
# (``XTZUSD``, ``ETHXBT``, ``ADAUSDT``), each with its length. Ordered longest
# first so the suffix match is unambiguous (``USDT`` before ``USD``). These are
# the only trailing quotes an altname pair uses; a 4-char ``Z``-prefixed fiat
# (``ZUSD``) only ever appears in the *legacy* 8-char form (handled separately),
# so it is deliberately NOT here — that ambiguity is exactly the ``XTZUSD`` bug
# (``XTZ`` + ``USD``, never ``XT`` + ``ZUSD``).
_KRAKEN_ALTNAME_QUOTES: tuple[str, ...] = (
    "USDT",
    "USDC",
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CAD",
    "AUD",
    "CHF",
    "DAI",
    "XBT",
    "ETH",
)

# --- Binance pair parsing -------------------------------------------------- #

# Binance concatenates legs with no separator (``BTCUSDT``, ``ETHBTC``) using
# canonical asset codes (no X/Z prefixes, no XBT alias — Binance Bitcoin is
# ``BTC``). To split a concatenated pair we match a trailing quote asset; the
# tuple is ordered **longest first** so the suffix match is unambiguous (e.g.
# ``FDUSD`` is tried before ``USD``, ``USDT`` before ``USD``).
_BINANCE_QUOTES: tuple[str, ...] = (
    "FDUSD",
    "USDT",
    "USDC",
    "TUSD",
    "BUSD",
    "DAI",
    "BTC",
    "ETH",
    "BNB",
    "EUR",
    "GBP",
    "TRY",
    "AUD",
    "BRL",
    "USD",
)


def normalise(asset: str) -> str:
    """Normalise a single venue asset code to its canonical form.

    Rules, applied in order:

    1. upper-case and strip surrounding whitespace;
    2. strip the leading legacy prefix **only** for a genuine Kraken legacy
       code — an ``X``-prefixed crypto in :data:`_KRAKEN_LEGACY_X` (``XXBT→XBT``,
       ``XETH→ETH``, ``XXTZ→XTZ``) or a ``Z``-prefixed fiat in
       :data:`_KRAKEN_LEGACY_Z` (``ZUSD→USD``, ``ZEUR→EUR``);
    3. apply ticker aliases — ``XBT→BTC``, ``XDG→DOGE``.

    A 4-char code that merely *starts* with ``X`` but is not a listed legacy
    code (e.g. ``XTZ`` is not 4 chars, ``XRPX`` is not legacy) is left intact —
    the previous "strip any leading X on any 4-char code" rule corrupted such
    tickers.

    Codes that are already canonical (``BTC``, ``ETH``, ``USD``, ``USDT``, ...)
    pass through unchanged.

    Parameters
    ----------
    asset : str
        A venue asset code (``"XXBT"``, ``"ZEUR"``, ``"XBT"``, ``"ETH"``, ...).

    Returns
    -------
    str
        The canonical asset code.

    Examples
    --------
    >>> normalise("XXBT")
    'BTC'
    >>> normalise("ZUSD")
    'USD'
    >>> normalise("XDG")
    'DOGE'
    >>> normalise("usdt")
    'USDT'
    >>> normalise("XTZ")   # Tezos, not an X-prefixed 'TZ'
    'XTZ'

    """
    code = asset.upper().strip()
    # Strip the legacy X/Z prefix ONLY for a genuine legacy code, looked up in
    # the explicit rosters. A modern altname that happens to start with X (e.g.
    # a hypothetical 4-char ticker) is left untouched — no blanket X-strip.
    if code in _KRAKEN_LEGACY_X or code in _KRAKEN_LEGACY_Z:
        code = code[1:]
    return _KRAKEN_TO_CANONICAL.get(code, code)


def _to_kraken_asset(canonical: str) -> str:
    """Render a canonical asset to its Kraken altname (``BTC→XBT``)."""
    return _CANONICAL_TO_KRAKEN.get(canonical, canonical)


@dataclass(frozen=True, slots=True)
class Symbol:
    """A canonical, venue-neutral trading pair.

    Frozen and hashable, so symbols are safe as dict keys / set members.
    ``base`` and ``quote`` are stored canonicalised and upper-cased.

    Parameters
    ----------
    base : str
        The base asset (what is bought/sold), e.g. ``"BTC"``.
    quote : str
        The quote asset (the price currency), e.g. ``"USD"``.

    Examples
    --------
    >>> str(Symbol("BTC", "USD"))
    'BTC/USD'
    >>> Symbol("xbt", "zusd") == Symbol("BTC", "USD")
    True

    """

    base: str
    quote: str

    def __post_init__(self) -> None:
        """Canonicalise both legs in place (frozen-safe via ``object.__setattr__``)."""
        object.__setattr__(self, "base", normalise(self.base))
        object.__setattr__(self, "quote", normalise(self.quote))

    def __str__(self) -> str:
        """``BASE/QUOTE``."""
        return f"{self.base}/{self.quote}"

    def to_venue_symbol(self, venue: str) -> str:
        """Render this canonical symbol back to a venue's pair code.

        Parameters
        ----------
        venue : str
            The venue name. Only ``"kraken"`` (case-insensitive) has a bespoke
            rendering today (altname form, ``XBTUSD`` / ``ETHXBT``); ``"binance"``
            and any other venue get the concatenated canonical legs (``BTCUSDT``).

        Returns
        -------
        str
            The venue pair code.

        Examples
        --------
        >>> Symbol("BTC", "USD").to_venue_symbol("kraken")
        'XBTUSD'
        >>> Symbol("ETH", "BTC").to_venue_symbol("kraken")
        'ETHXBT'
        >>> Symbol("BTC", "USDT").to_venue_symbol("binance")
        'BTCUSDT'

        """
        if venue.lower() == "kraken":
            return f"{_to_kraken_asset(self.base)}{_to_kraken_asset(self.quote)}"
        # Binance (and any other venue) use the bare concatenated canonical legs;
        # the explicit branch keeps the no-alias guarantee visible.
        return f"{self.base}{self.quote}"


def parse_kraken_pair(pair: str) -> Symbol:
    """Parse a Kraken pair string into a canonical :class:`Symbol`.

    Handles both the legacy X/Z-prefixed form (``XXBTZUSD``, ``XETHZEUR``) and
    the modern altname form (``XBTUSD``, ``ETHUSD``, ``ETHXBT``).

    The split strategy:

    * a separator (``/``, ``-``, ``_``) is honoured if present;
    * an 8-char *legacy* pair whose **both** halves are genuine legacy codes
      (in :data:`_KRAKEN_LEGACY_X` / :data:`_KRAKEN_LEGACY_Z`) splits 4/4
      (``XXBT`` + ``ZUSD``);
    * otherwise it is a modern altname: the quote is the longest trailing code
      in :data:`_KRAKEN_ALTNAME_QUOTES` (``USDT`` before ``USD``), the remainder
      is the base.

    The altname quote table deliberately excludes the 4-char ``Z``-prefixed
    fiats — those only appear in the legacy form — so ``XTZUSD`` splits as
    ``XTZ`` + ``USD`` (Tezos), not the old, wrong ``XT`` + ``ZUSD``.

    Parameters
    ----------
    pair : str
        A Kraken pair string.

    Returns
    -------
    Symbol
        The canonical symbol.

    Raises
    ------
    ValueError
        If the pair cannot be split into a base and a quote.

    Examples
    --------
    >>> str(parse_kraken_pair("XXBTZUSD"))
    'BTC/USD'
    >>> str(parse_kraken_pair("XETHZEUR"))
    'ETH/EUR'
    >>> str(parse_kraken_pair("ETHUSD"))
    'ETH/USD'
    >>> str(parse_kraken_pair("ETHXBT"))
    'ETH/BTC'
    >>> str(parse_kraken_pair("XTZUSD"))
    'XTZ/USD'

    """
    raw = pair.strip()
    # Explicit separator wins.
    for sep in ("/", "-", "_"):
        if sep in raw:
            base, quote = raw.split(sep, 1)
            return Symbol(base, quote)

    code = raw.upper()
    # Legacy 8-char form: XXBT + ZUSD (fiat quote), XETH + XXBT (crypto quote),
    # ... split 4/4, but ONLY when both halves are genuine legacy codes — the
    # base is always X-crypto; the quote is either a Z-fiat or an X-crypto. This
    # guard stops an 8-char *altname* (e.g. ``MATICUSD``) being mis-split.
    if (
        len(code) == 8
        and code[:4] in _KRAKEN_LEGACY_X
        and (code[4:] in _KRAKEN_LEGACY_Z or code[4:] in _KRAKEN_LEGACY_X)
    ):
        return Symbol(code[:4], code[4:])

    # Modern altname form: longest trailing quote in the explicit table wins
    # (USDT before USD), leaving a non-empty base. No 4-char Z-fiat here, so the
    # XTZ/USD boundary is unambiguous.
    for quote in _KRAKEN_ALTNAME_QUOTES:
        if code.endswith(quote) and len(code) > len(quote):
            return Symbol(code[: -len(quote)], quote)

    raise ValueError(f"cannot parse Kraken pair {pair!r}")


def parse_binance_symbol(pair: str) -> Symbol:
    """Parse a Binance pair string into a canonical :class:`Symbol`.

    Binance concatenates the legs with no separator and uses canonical asset
    codes (``BTCUSDT``, ``ETHBTC``, ``BNBUSDT``) — no Kraken-style ``X``/``Z``
    prefixes and no ``XBT`` alias (Binance Bitcoin is ``BTC``).

    The split strategy:

    * a separator (``/``, ``-``, ``_``) is honoured if present;
    * otherwise the first quote in :data:`_BINANCE_QUOTES` (longest first) that
      the upper-cased string **ends with**, leaving a non-empty base, wins.

    :meth:`Symbol.__post_init__` canonicalises both legs, but :func:`normalise`
    passes Binance codes through unchanged, so no Binance alias table is needed.

    Parameters
    ----------
    pair : str
        A Binance pair string (``"BTCUSDT"``, ``"ETHBTC"``, ``"BTC/USDT"``).

    Returns
    -------
    Symbol
        The canonical symbol.

    Raises
    ------
    ValueError
        If the pair cannot be split into a base and a quote.

    Examples
    --------
    >>> str(parse_binance_symbol("BTCUSDT"))
    'BTC/USDT'
    >>> str(parse_binance_symbol("ETHBTC"))
    'ETH/BTC'
    >>> str(parse_binance_symbol("ETHFDUSD"))
    'ETH/FDUSD'

    """
    raw = pair.strip()
    # Explicit separator wins.
    for sep in ("/", "-", "_"):
        if sep in raw:
            base, quote = raw.split(sep, 1)
            return Symbol(base, quote)

    code = raw.upper()
    # Longest-first suffix match so e.g. ``FDUSD`` wins over ``USD``.
    for quote in _BINANCE_QUOTES:
        if code.endswith(quote) and len(code) > len(quote):
            return Symbol(code[: -len(quote)], quote)

    raise ValueError(f"cannot parse Binance pair {pair!r}")


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradeable instrument: a :class:`Symbol` plus venue trading metadata.

    Frozen and hashable. ``price_precision`` / ``qty_precision`` are the number
    of decimal places the venue accepts for price and quantity respectively;
    both are optional (unknown until the venue's metadata is loaded).

    Parameters
    ----------
    symbol : Symbol
        The canonical pair.
    price_precision : int, optional
        Number of decimal places allowed for the price.
    qty_precision : int, optional
        Number of decimal places allowed for the quantity / volume.

    Examples
    --------
    >>> inst = Instrument(Symbol("BTC", "USD"), price_precision=1, qty_precision=8)
    >>> str(inst)
    'BTC/USD'
    >>> inst.price_precision
    1

    """

    symbol: Symbol
    price_precision: int | None = None
    qty_precision: int | None = None

    def __str__(self) -> str:
        """The underlying symbol's ``BASE/QUOTE``."""
        return str(self.symbol)
