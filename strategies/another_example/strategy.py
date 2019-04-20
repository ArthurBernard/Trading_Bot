#!/usr/bin/env python3
# coding: utf-8

import numpy as np
from strategy_manager.data_requests import DataRequests


__all__ = ['get_signal']


def get_order_params(data, *args, **kwargs):
    """ Return signal, price and volume """
    signal = get_signal(data, *args, **kwargs)
    price = get_price(data, signal, *args, **kwargs)

    return signal, price, 1.


def get_signal(*args, **kwargs):
    """ Call example strategy and return signal """
    return example_random_strat(**kwargs)


def example_random_strat(**kwargs):
    """ A dummy exemple that return a random signal """
    signals = [-1, 0, 1]
    return np.random.choice(signals)


def get_price(data, signal, *args, **kwargs):
    """ Compute price """
    req = DataRequests("https://api.kraken.com/0/public", stop_step=1)
    ans = req.get_data('Ticker', pair='XBTUSD')
    marge = float(np.random.rand(1) - 0.4) / 10.

    if signal > 0:
        price = float(ans['result']['XXBTZUSD']['a'][0])
        price /= 1 + marge

    elif signal < 0:
        price = float(ans['result']['XXBTZUSD']['b'][0])
        price *= 1 + marge

    else:
        price = float(ans['result']['XXBTZUSD']['c'][0])

    return round(price, 0)
