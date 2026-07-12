"""Display-currency resolution + conversion — one converted truth per amount.

The api-completeness epic's leaf 03 (pinned decision, ``doc/dev/plans/
api-completeness/00-plan.md``): the engine trades in whatever quote currency
each instrument declares (Binance ``USDT``, Kraken ``USD``, ...), but a
dashboard reading *across* venues needs a single unit to read totals in. This
module is the one place that answers "what currency, and what number" — the
API's row/aggregate serializers (:mod:`trading_bot.interfaces.api.app`) both
call through here so every consumer agrees (never two different converted
figures for the same amount).

Two independent questions, two functions
-----------------------------------------
* :func:`resolve_currency` — **the label**: which currency a given exchange's
  rows are *presented* under. The global :attr:`~trading_bot.application.
  config.AppConfig.display_currency` (e.g. ``"USD"``), unless
  :attr:`~trading_bot.application.config.AppConfig.display_currency_overrides`
  names a different currency for that specific exchange (e.g.
  ``{"binance": "USDT"}`` — Binance trades USDT-quoted, so its rows are
  labelled ``"USDT"`` rather than converted-and-labelled ``"USD"``).
* :func:`convert` — **the number**: every ``*_display`` amount is expressed in
  the *one* global ``display_currency`` (the single numeraire the "one truth
  for every consumer" decision calls for) — never in a row's overridden label.
  ``conversion_rates`` declares, per source quote currency, how many units of
  the global ``display_currency`` one unit of that quote is worth; the
  ``display_currency`` itself needs no entry (identity, rate ``1`` implied).

  This means an override is only numerically inert when its named currency is
  *itself* pegged 1:1 to the global ``display_currency`` (the common case: a
  dollar-stablecoin quote against a ``"USD"`` global default, declared with an
  explicit identity rate, e.g. ``conversion_rates={"USDT": "1"}``) — the label
  reads ``"USDT"`` and the converted figure is numerically unchanged from the
  native one. Overriding a row's label to a currency whose rate is *not* ``1``
  would still convert the underlying amount into the global
  ``display_currency`` — the label and the number would then disagree, so
  operators should only override a label alongside a rate that keeps it
  truthful.

Never guess a rate (pinned)
----------------------------
:func:`convert` returns ``None`` — never a fabricated number — whenever the
source currency is unknown (a mixed-quote aggregate, ``quote is None``) or has
no declared rate. The API renders that as JSON ``null``, alongside the
untouched native figure, rather than silently omitting the conversion or
inventing one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trading_bot.domain.money import Money, mul

if TYPE_CHECKING:
    from trading_bot.application.config import AppConfig

__all__ = ["resolve_currency", "convert"]


def resolve_currency(exchange: str | None, config: AppConfig) -> str:
    """The currency an exchange's rows are labelled under (the ``display_currency`` field).

    Parameters
    ----------
    exchange : str or None
        The row's venue key (e.g. ``"binance"``). ``None`` for an aggregate row
        that spans every exchange (e.g. a KPI ``level="total"`` row) — there is
        no single exchange to look up an override for, so it falls back to the
        global default exactly like an unlisted exchange would.
    config : AppConfig
        The running config; consulted for
        :attr:`~trading_bot.application.config.AppConfig.display_currency_overrides`
        and the :attr:`~trading_bot.application.config.AppConfig.display_currency`
        fallback.

    Returns
    -------
    str
        ``config.display_currency_overrides[exchange]`` when set, else
        ``config.display_currency``.

    """
    if exchange is None:
        return config.display_currency
    return config.display_currency_overrides.get(exchange, config.display_currency)


def convert(
    amount: Money | None, from_ccy: str | None, config: AppConfig
) -> Money | None:
    """Convert ``amount`` (in ``from_ccy``) into the global ``display_currency``.

    Exact :class:`~decimal.Decimal` arithmetic throughout — never ``float``.
    Never guesses: an unconvertible amount renders ``None`` (JSON ``null``),
    not a fabricated figure.

    Parameters
    ----------
    amount : Money or None
        The native-quote figure to convert. ``None`` (e.g. an unmarked
        position's ``value``/``unrealised``) passes through as ``None``.
    from_ccy : str or None
        The currency ``amount`` is denominated in (a position row's
        ``fee_ccy``, a strategy/KPI row's ``quote``). ``None`` when the source
        is unknown or ambiguous (a KPI/strategy aggregate folding units that
        mix quote currencies — the dashboard's "mixed") — never guessed, so
        conversion is refused.
    config : AppConfig
        The running config; consulted for
        :attr:`~trading_bot.application.config.AppConfig.display_currency` (the
        conversion target, and the implied-identity source) and
        :attr:`~trading_bot.application.config.AppConfig.conversion_rates` (one
        unit of ``from_ccy`` expressed in ``display_currency``).

    Returns
    -------
    Money or None
        ``amount`` unchanged when ``from_ccy`` already *is* the display
        currency (identity, no rate needed); ``amount * rate`` when a rate is
        declared for ``from_ccy``; ``None`` when ``amount`` or ``from_ccy`` is
        ``None``, or no rate is declared for ``from_ccy``.

    """
    if amount is None or from_ccy is None:
        return None
    if from_ccy == config.display_currency:
        return amount
    rate = config.conversion_rates.get(from_ccy)
    if rate is None:
        return None
    return mul(amount, rate)
