#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import time
import sys

# Import external packages

# Import internal packages
from manager import StrategyManager
from data_requests import DataManager
from tools.utils import load_config_params
from orders_manager import SetOrder
from results_manager import print_results

__all__ = ['run_bot']


def check(*args, **kwargs):
    """ Helper to debug, it prints args and kwargs and ask you if you want
    to quit.

    """
    for arg in args:
        print(arg)
    for key, arg in kwargs.items():
        print('{} : {}'.format(str(key), str(arg)))
    a = input('press q to quit else continue')
    if a.lower() == 'q':
        sys.exit()
    return 0


def run_bot(id_strat, path='strategy_manager/strategies/'):
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
    check(path)
    data_cfg = load_config_params(path + id_strat + '.cfg')
    check(**data_cfg)

    # Get parameters for strategy manager object
    strat_manager_params = data_cfg['strat_manager_instance']
    # Set strategy manager configuration
    strat_manager = StrategyManager(**strat_manager_params)
    check(strat_manager)

    # Get parameters for data requests
    data_requests_params = data_cfg['get_data_instance']
    # Set data requests manager configuration
    data_manager = DataManager(**data_requests_params)
    check(data_manager)

    # Get parameters for pre order configuration
    pre_order_params = data_cfg['pre_order_instance']
    # Set pre order configuration
    order_manager = SetOrder(**pre_order_params)
    check(order_manager)

    # Get order parameters
    order_params = data_cfg['order_instance']

    # Get parameters for strategy function
    strat_args = data_cfg['strategy_instance']['args_params']
    strat_kwargs = data_cfg['strategy_instance']['kwargs_params']

    # The bot start to run
    try:
        for t in strat_manager(*strat_args, **strat_kwargs):
            print('{}th iteration'.format(t))
            # Get data from data base
            data = data_manager.get_data()
            check(data)

            # Compute and get signal' strategy
            signal = strat_manager.get_signal(data)
            check(signal)

            # Set order
            ans = order_manager.set_order(signal, **order_params)
            check(ans)

            # Check to verify and debug
            id_order = ans['userref']
            status = order_manager.get_status_order(id_order)
            check(status)

            # TODO : compute, print and save some statistics
            print_results(ans)

            # Get current pos
            current_pos = order_manager.current_pos
            print(current_pos)
            # TODO : check if current position is ok
            pass
        else:
            print('All is good')

    except Exception as error:
        # TODO : how manage unknown error
        time_str = time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time()))
        txt = '\nUNKNOWN ERROR\n'
        txt += 'In {} script '.format(sys.argv[0])
        txt += 'for {} strat id '.format(id_strat)
        txt += 'at {} UTC, '.format(time_str)
        txt += 'the following error occurs:\n'
        txt += '{}: {}\n'.format(str(type(error)), str(error))
        print(txt)
        with open('errors/{}.log'.format(sys.argv[0]), 'a') as f:
            f.write(txt)

    finally:
        # TODO : ending with save some statistics and others
        # TODO : save current position
        print('\nBot stopped. See you soon !\n')


if __name__ == '__main__':
    # print(sys.argv[2])
    # sys.exit()
    run_bot(sys.argv[1])
