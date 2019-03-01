#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import sys
from pickle import Pickler, Unpickler

# Import local packages
import strat_manager as sm
from utils import load_config_params

# if __name__ == '__main__':

# Set strategy
# Example:
# def strat_long_only():
#     return 1

# Set parameters
strat_name = sys.argv[1]
#underlying = sys.argv[2]
#frequency = sys.argv[3]
#extra_params = sys.argv[4]

# Get configuration parameters
data_cfg = load_config_params('path/' + strat_name + '.cfg')

# Set parameters
underlying = data_cfg['strat_manager_instance']['underlying']
frequency = data_cfg['strat_manager_instance']['frequency']
strat_name = data_cfg['strat_manager_instance']['strat_name']
volume = data_cfg['strat_manager_instance']['volume']

extra_instance = data_cfg['extra_instance']

args = data_cfg['args_params']
kwargs = data_cfg['kwargs_params']

# Set strategy manager object
strat = sm.StrategyManager(
    timestep=frequency, 
    underlying=underlying, 
    strategy=strat_name, 
    volume=volume, 
    **extra_instance
):

# Load data

# Starting to run
for t in strat(*args, **kwargs):
    # TODO : iterative methods
    # 1 - Get data
    # 2 - Compute signal
    # 3 - Execute order
    # 4 - 

# TODO : save extra_params