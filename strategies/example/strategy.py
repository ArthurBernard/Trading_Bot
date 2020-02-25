#!/usr/bin/env python3
# coding: utf-8

""" Example of random strategy. """

# Import built-in packages

# Import external packages
import numpy as np
import fynance as fy

# Import internal packages
from trading_bot.data_requests import DataRequests

__all__ = ['get_signal']


def get_order_params(data, *args, **kwargs):
    """ Return signal, price and volume. """
    # Get parameters
    params = {}

    # Set signal
    signal = get_signal(data, *args, **kwargs)

    # Set paramaters
    params['price'] = get_price(data, signal, *args, **kwargs)
    # params['volume'] *= 1.5 + signal

    return signal, params


def get_signal(data, *args, **kwargs):
    """ Compute signal. """
    return int(np.random.choice(args))


def get_coef_volume(data, *args, **kwargs):
    """ Compute volume. """
    if 'c' in data.columns:
        series = data.loc[:, 'c']

    elif 'o' in data.columns:
        series = data.loc[:, 'o']

    else:
        series = data.iloc[:, 0]

    iv = set_iso_vol(series.values, *args, **kwargs)

    return iv


def get_price(data, signal, *args, **kwargs):
    """ Compute price. """
    req = DataRequests("https://api.kraken.com/0/public", stop_step=1)
    ans = req.get_data('Ticker', pair='ETHUSD')
    marge = float(np.random.rand(1) - 0.4) / 10.

    if signal > 0:
        price = float(ans['result']['XETHZUSD']['a'][0])
        price /= 1 + marge

    elif signal < 0:
        price = float(ans['result']['XETHZUSD']['b'][0])
        price *= 1 + marge

    else:
        price = float(ans['result']['XETHZUSD']['c'][0])

    return round(price, 2)


def set_iso_vol(series, *args, target_vol=0.20, leverage=1.,
                period=252, half_life=11, **kwargs):
    """ Compute iso-volatility coefficient.

    Iso-volatility coefficient is computed such that to target a
    specified volatility of underlying.

    Parameters
    ----------
    series : np.ndarray[ndim=1, dtype=np.float64]
        Series of prices of underlying.
    target_vol : float, optional
        Volatility to target, default is `0.20` (20 %).
    leverage : float, optional
        Maximum of the iso-volatility coefficient, default is `1.`.
    period : int, optional
        Number of trading day per year, default is `252`.
    half_life : int, optional
        Number of day to compute exponential volatility, default is `11`.

    Returns
    -------
    iv_coef : float
        Iso-volatility coefficient between `0` and `leverage`.

    """
    # period = int(period * 86400 / frequency)
    # print('ok')
    iv_series = fy.iso_vol(series, target_vol=target_vol, leverage=leverage,
                           period=period, half_life=half_life)

    return iv_series[-1]
