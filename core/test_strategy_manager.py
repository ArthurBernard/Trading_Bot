#!/usr/bin/env python3
# coding: utf-8 

# Built-in packages
import time

# External packages
import numpy as np
import pytest

# Internal packages
from strategy_manager import StrategyManager
from utils import load_config_params

@pytest.fixture()
def set_variables():
    # Get configuration parameters
    data_cfg = load_config_params('../strategies/example_function.cfg')
    return data_cfg
    

def test_StrategyManager(set_variables):
    # Set parameters
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
    time.wait(time.time() % frequency)
    t0 = time.time()
    for t in sm(*args, **kwargs):
        t1 = time.time()
        assert t0 + frequency + 2 > t1
        assert t0 + frequency - 2 < t1
        break

    # Test another method
    pass