#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import time
import sys

# Import external packages

# Import internal packages
from manager import StrategyManager
# from data_requests import DataManager
from tools.utils import load_config_params, dump_config_params, get_df
from tools.time_tools import now
from orders_manager import SetOrder
from results_manager import print_results, set_order_results
from results_manager import update_order_hist, update_result_hist

__all__ = ['run_bot']


def check(*args, **kwargs):
    """ Helper to debug, it prints args and kwargs and ask you if you want
    to quit.

    """
    for arg in args:
        print(arg, '\n')
    for key, arg in kwargs.items():
        print('{} : {}\n'.format(str(key), str(arg)))
    a = input('\npress q to quit else continue\n')
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

    data_cfg = load_config_params(path + id_strat + '.cfg')
    print('\n Check cfg:\n', '-' * 9)
    check(**data_cfg)

    # Get parameters for strategy manager object
    SM_params = data_cfg['strat_manager_instance']
    # Set strategy manager configuration
    SM = StrategyManager(**SM_params.copy())

    # Get parameters for data requests
    data_requests_params = data_cfg['get_data_instance'].copy()
    # Set data requests manager configuration
    SM.set_data_manager(**data_requests_params.copy())

    # Get parameters for pre order configuration
    pre_order_params = data_cfg['pre_order_instance'].copy()
    # Set pre order configuration
    OM = SetOrder(frequency=SM_params['frequency'], **pre_order_params)

    # Get order parameters
    order_params = data_cfg['order_instance']
    # Get parameters for strategy function
    args = data_cfg['strategy_instance']['args_params']
    kwargs = data_cfg['strategy_instance']['kwargs_params']

    # The bot start to run
    try:
        for s, p, v in SM(*args.copy(), **kwargs.copy()):
            print('{}th iteration'.format(SM.t))

            print('\n Check spv:\n', '-' * 9)
            check(s, p, v)

            # Set order
            order_params['volume'] *= float(v)
            outputs = OM.set_order(s, price=p, **order_params.copy())
            print('\n Check out:\n', '-' * 9)
            check(*outputs)

            # Check to verify and debug
            if not order_params['validate']:
                for output in outputs:
                    id_order = output['result']['userref']
                    status = OM.get_status_order(id_order)
                    check(status)

            # TODO : compute, print and save some statistics
            # Clean outputs
            outputs = set_order_results(outputs)
            print('\n Check output:\n', '-' * 12)
            check(*outputs)

            # Update result historic
            update_result_hist(outputs, id_strat, path=path)

            # Update order historic
            update_order_hist(outputs, id_strat, path=path)

            # TODO : Print results
            print_results(outputs)

            # Get current pos
            current_pos = OM.current_pos
            print('Current position is:', current_pos)
            # TODO : check if current position is ok
            data_cfg['pre_order_instance']['current_pos'] = float(
                OM.current_pos
            )
            # TODO : check if current volume is ok
            data_cfg['pre_order_instance']['current_vol'] = abs(float(
                OM.current_vol
            ))
            pass
        else:
            print('All is good')

    except Exception as error:
        # DEBUG
        raise error
        # TODO : how manage unknown error
        # Report error TODO : Improve
        time_str = time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(now()))
        txt = '\nUNKNOWN ERROR\n'
        txt += 'In {} script '.format(sys.argv[0])
        txt += 'for {} strat id '.format(id_strat)
        txt += 'at {} UTC, '.format(time_str)
        txt += 'the following error occurs:\n'
        txt += '{}: {}\n'.format(str(type(error)), str(error))
        print(txt)

        with open(path + sys.argv[1] + '.log', 'a') as f:
            f.write(txt)

    finally:
        # DEGUG
        df_ord = get_df(path, id_strat + '_ord_hist', '.dat')
        df_res = get_df(path, id_strat + '_res_hist', '.dat')
        print('Historic:', '-' * 9, df_ord.head(), df_res.head(), sep='\n')
        # TODO : ending with save some statistics and others
        # Save current position and volume
        dump_config_params(data_cfg, path + id_strat + '.cfg')
        print('\nBot stopped. See you soon !\n')


if __name__ == '__main__':
    txt = '\nStrategy {} starts to run !\n'.format(sys.argv[1])
    txt += '-' * (len(txt) - 2) + '\n'
    print(txt)
    run_bot(sys.argv[1])
