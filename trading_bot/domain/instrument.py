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


def normalise(asset: str) -> str:
    """Normalise a single venue asset code to its canonical form.

    Rules, applied in order:

    1. upper-case and strip surrounding whitespace;
    2. on a **4-character** legacy code, strip a leading ``X`` (crypto) or
       ``Z`` (fiat) prefix — ``XXBT→XBT``, ``XETH→ETH``, ``ZUSD→USD``,
       ``ZEUR→EUR``;
    3. apply ticker aliases — ``XBT→BTC``, ``XDG→DOGE``.

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

    """
    code = asset.upper().strip()
    # Strip the legacy X/Z prefix only on 4-char codes (XXBT, XETH, ZUSD...).
    # Modern altnames (USDT, TRX) and bare aliases (XBT, XDG) are 3-4 chars
    # without a doubled/fiat prefix, so guard on length == 4 and a known prefix.
    if len(code) == 4 and code[0] in ("X", "Z"):
        stripped = code[1:]
        # Only strip when it actually yields a known crypto (X-prefixed) or a
        # known fiat (Z-prefixed); otherwise keep the original (e.g. a genuine
        # 4-letter ticker would be left intact — none in our universe).
        if code[0] == "Z" and stripped in _FIAT:
            code = stripped
        elif code[0] == "X":
            code = stripped
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
            rendering today (altname form, ``XBTUSD`` / ``ETHXBT``); any other
            venue gets the concatenated canonical legs.

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

        """
        if venue.lower() == "kraken":
            return f"{_to_kraken_asset(self.base)}{_to_kraken_asset(self.quote)}"
        return f"{self.base}{self.quote}"


def parse_kraken_pair(pair: str) -> Symbol:
    """Parse a Kraken pair string into a canonical :class:`Symbol`.

    Handles both the legacy X/Z-prefixed form (``XXBTZUSD``, ``XETHZEUR``) and
    the modern altname form (``XBTUSD``, ``ETHUSD``, ``ETHXBT``).

    The split strategy:

    * a separator (``/``, ``-``, ``_``) is honoured if present;
    * an 8-char legacy pair splits 4/4 (``XXBT`` + ``ZUSD``);
    * otherwise the quote is taken as a trailing known fiat (3-char) or a
      trailing ``XBT``/``XXBT`` crypto-quote, with the remainder as base.

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

    """
    raw = pair.strip()
    # Explicit separator wins.
    for sep in ("/", "-", "_"):
        if sep in raw:
            base, quote = raw.split(sep, 1)
            return Symbol(base, quote)

    code = raw.upper()
    # Legacy 8-char form: XXBT + ZUSD, XETH + ZEUR, ... split 4/4.
    if len(code) == 8 and code[0] in ("X", "Z") and code[4] in ("X", "Z"):
        return Symbol(code[:4], code[4:])

    # Altname form: try a trailing fiat quote (after normalisation), then a
    # trailing crypto quote (XBT). Longest plausible quote first.
    for qlen in (4, 3):
        if len(code) > qlen:
            base_raw, quote_raw = code[:-qlen], code[-qlen:]
            quote = normalise(quote_raw)
            if quote in _FIAT or quote in {"BTC", "USDT", "USDC", "DAI"}:
                return Symbol(base_raw, quote_raw)

    raise ValueError(f"cannot parse Kraken pair {pair!r}")


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
