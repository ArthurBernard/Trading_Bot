#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import json 
import time
import sys

# Import external packages

# Import internal packages
from . import StrategyManager

__all__ = ['run_bot']

def run_bot(strat_id, path='../strategies/'):
	""" Run a bot for specified configuration file.

	Parameters
	----------
	strat_id : str
	    A strat id is the name of the corresonding configuration file.
	path : str
	    Path where is the configuration file.

	"""
	if path[-1] != '/':
		path += '/'
    data_cfg = load_config_params(path + strat_id + '.cfg')
    
    # Get parameters for strategy manager object
    strat_manager_params = data_cfg['strat_manager_instance']
    # Get parameters for strategy function
    strat_args = data_cfg['strategy_instance']['args_params']
    strat_kwargs = data_cfg['strategy_instance']['kwargs_params']
    # Init strategy manager
    strat_manager = StrategyManager(**strat_manager_params)
    
    # Get parameters for data requests
    data_requests_params = data_cfg['get_data_instance']
    data_requests_kwargs = data_requests_params.pop('kwargs_params')
    # Set data requests configuration
    strat_manager.set_data_loader(**data_requests_params, **data_requests_kwargs)
    
    # Get parameters for pre order configuration
    pre_order_params = data_cfg['pre_order_instance']
    # Set pre order configuration
    strat_manager.set_pre_order(**pre_order_params)
    
    # Get order parameters
    order_params = data_cfg['order_instance']
    order_args = order_params.pop('args_params')
    order_kwargs = order_params.pop('kwargs_params')
    # Set order configuration
    strat_manager.set_order(*order_args, **order_params, **order_kwargs)

    # The bot start to run
    try:
        for t in strat_manager(*strat_args, **strat_kwargs):
        	print('{}th iteration'.format(t))
        	# TODO : compute, print and save some statistics
        else:
            print('All is good')

    except Exception as error:
    	# TODO : how manage unknown error
    	time_str = time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time()))
    	txt = '\nUNKNOWN ERROR\n'
    	txt += 'In {} script '.format(sys.argv[0])
    	txt += 'for {} strat id '.format(strat_id)
    	txt += 'at {} UTC, '.format(time_str)
    	txt += 'the following error occurs:\n'
    	txt += '{}: {}\n'.format(str(type(error)), str(error))
        print(txt)
        with open('errors/{}.log'.format(sys.argv[0]), 'a') as f:
            f.write(txt)
    
    finally:
        # TODO : ending with save some statistics and others
        print('\nBot stoped\nSee you soon')

if __name__ == '__main__':
	run_bot(sys.argv[1])