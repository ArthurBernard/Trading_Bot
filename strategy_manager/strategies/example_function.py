#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np


def get_signal(*args, **kwargs):
    """ Call example strategy and return signal """
    return example_random_strat(**kwargs)


def example_random_strat(**kwargs):
    """ A dummy exemple that return a random signal """
    signals = [-1, 0, 1]
    return np.random.choice(signals)
