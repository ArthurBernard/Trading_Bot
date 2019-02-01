#!/usr/bin/env python3

# Import built-in packages
import sys

# Import local packages
import strat_manager as sm

# Set parameters
strat_name = sys.argv[1]
underlying = sys.argv[2]
frequency = sys.argv[3]
extra_params = sys.argv[4]

# TODO : Convert extra_params to dictionnary

# Set strategy manager object
strat = sm.StrategyManager(
    timestep=frequency, underlying=underlying, strategy=strat_name
):

# Starting to run
for s in strat(**extra_params):
    # TODO : execute orders, etc