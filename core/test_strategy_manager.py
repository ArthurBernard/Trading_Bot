#!/usr/bin/env python3
# coding: utf-8 

# Built-in packages
import time
import os

# External packages
import numpy as np
import pytest

# Internal packages
from .strategy_manager import StrategyManager
from .tools.utils import load_config_params

@pytest.fixture()
def set_variables():
    # Get configuration parameters
    data_cfg = load_config_params('./strategies/example_function.cfg')
    return data_cfg
    

def test_StrategyManager(set_variables):
    # Set parameters
    data_cfg = set_variables
    underlying = data_cfg['strat_manager_instance']['underlying']
    frequency = data_cfg['strat_manager_instance']['frequency']
    strat_name = data_cfg['strat_manager_instance']['strat_name']
    volume = data_cfg['strat_manager_instance']['volume']

    extra_instance = data_cfg['extra_instance']

    args = data_cfg['args_params']
    kwargs = data_cfg['kwargs_params']

    sm = StrategyManager(timestep=frequency, underlying=underlying, 
        strategy=strat_name, volume=volume, **extra_instance)
    
    # Test initialisation
    assert sm.strategy == strat_name
    assert sm.timestep == frequency
    assert sm.underlying == underlying
    assert sm.volume == volume

    # Test __call__ method
    assert isinstance(sm(*args, **kwargs), StrategyManager)

    # Test __iter__ method
    time.sleep(int(time.time()) % frequency)
    t0 = int(time.time())
    #time.sleep(frequency)
    for t in sm(*args, **kwargs):
        sm.STOP = 3
        print('{} th iteration is ok'.format(t))
        t1 = int(time.time())
        assert t0 + frequency + 2 > t1
        assert t0 + frequency - 2 < t1
        t0 = t1

    # Test another method
    pass