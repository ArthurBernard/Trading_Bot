#!/usr/bin/env python3
# coding: utf-8

# Built-in packages
import time
import os

# External packages
import numpy as np
import pytest

# Internal packages
from strategy_manager.manager import StrategyManager
from strategy_manager.tools.utils import load_config_params


@pytest.fixture()
def set_variables():
    # Get configuration parameters
    data_cfg = load_config_params('./strategies/example_function.cfg')
    return data_cfg


def test_StrategyManager(set_variables):
    # Set parameters
    data_cfg = set_variables

    # Set StrategyManager parameters
    start_manager_parameters = data_cfg['strat_manager_instance']
    underlying = data_cfg['strat_manager_instance']['underlying']
    frequency = data_cfg['strat_manager_instance']['frequency']
    strat_name = data_cfg['strat_manager_instance']['strat_name']

    # Set parameters for strategy function
    strat_args = data_cfg['strategy_instance']['args_params']
    strat_kwargs = data_cfg['strategy_instance']['kwargs_params']

    sm = StrategyManager(STOP=2, **start_manager_parameters)

    # Test initialisation
    assert sm.strat_name == strat_name
    assert sm.frequency == frequency
    assert sm.underlying == underlying

    # Test __call__ method
    assert isinstance(sm(*strat_args, **strat_kwargs), StrategyManager)

    # Test __iter__ method
    t0 = int(time.time())
    for t in sm(*strat_args, **strat_kwargs):
        print(r'${}^{th}$ iteration is ok'.format(t))
        t1 = int(time.time())
        assert t0 + frequency + 3 > t1
        assert t0 + frequency - 3 < t1
        t0 = t1

    # Test get_data
    # Get parameters for data requests
    data_requests_params = data_cfg['get_data_instance']
    data_requests_args = data_requests_params.pop('args_params')
    data_requests_kwargs = data_requests_params.pop('kwargs_params')
    # Set data requests configuration
    sm.set_data_loader(
        *data_requests_args,
        **data_requests_params,
        **data_requests_kwargs
    )

    # Test another method
    pass
