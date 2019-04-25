#!/usr/bin/env python3
# coding: utf-8

# Import external packages
import numpy as np

# Import internal packages
from tools.utils import load_config_params


__all__ = ['get_signal']


def get_order_params(data, *args, **kwargs):
    """ Return signal, price and volume """
    # Get parameters
    data_cfg = load_config_params(
        './strategies/another_example/configuration.yaml'
    )
    params = data_cfg['order_instance']

    # Set signal
    signal = get_signal(data, *args, **kwargs)

    return signal, params


def get_signal(*args, **kwargs):
    """ Call example strategy and return signal """
    return example_random_strat(**kwargs)


def example_random_strat(**kwargs):
    """ A dummy exemple that return a random signal """
    signals = [-1, 0, 1]
    return np.random.choice(signals)
