#!/usr/bin/env python3

# Import built-in packages
import sys
from pickle import Pickler, Unpickler

# Import local packages
import strat_manager as sm

# Set parameters
strat_name = sys.argv[1]
#underlying = sys.argv[2]
#frequency = sys.argv[3]
#extra_params = sys.argv[4]

# Set parameters
with open('path/' + strat_name +'_parameters', 'rb') as f:
    params = Unpickler(f).load()

underlying = params['underlying']
frequency = params['frequency']
strat_name = params['strat_name']
volume = params['volume']

with open('path/' + strat_name +'_extra_parameters', 'rb') as f:
    extra_params = Unpickler(f).load()

# Set strategy manager object
strat = sm.StrategyManager(
    timestep=frequency, underlying=underlying, strategy=strat_name
):

# Starting to run
for s in strat(**extra_params):
    # TODO : iterative methods
    # 1 - Get data
    # 2 - Compute signal
    # 3 - Execute order
    # 4 - 

# TODO : save extra_params