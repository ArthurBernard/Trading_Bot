#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import time
import sys
import logging
# from logging.handlers import RotatingFileHandler

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
    txt = ''

    for arg in args:
        txt += str(arg) + '\n'

    for key, arg in kwargs.items():
        txt += '{} : {}\n'.format(str(key), str(arg))

    a = '0'  # input('\npress q to quit else continue\n')

    if a.lower() == 'q':
        sys.exit()

    return txt


def run_bot(id_strat, path='strategies/'):
    """ Run a bot for specified configuration file.

    Parameters
    ----------
    strat_id : str
        A strat id is the name of the corresonding configuration file.
    path : str
        Path where is the configuration file.

    """
    logger = logging.getLogger('strat_man')

    if path[-1] != '/':
        path += '/'

    # Load data configuration
    data_cfg = load_config_params(path + id_strat + '/configuration.yaml')

    # Get parameters for strategy manager object
    SM_params = data_cfg['strat_manager_instance']
    # Set strategy manager configuration
    SM = StrategyManager(**SM_params.copy())

    # Get parameters for data requests
    data_requests_params = data_cfg['get_data_instance'].copy()
    # Set data requests manager configuration
    SM.set_data_manager(**data_requests_params.copy())

    # Get parameters for pre order configuration
    pre_order_params = data_cfg['pre_order_instance']
    # Set pre order configuration
    OM = SetOrder(**pre_order_params)

    # Get order parameters
    order_params = data_cfg['order_instance']
    vol = order_params.pop('volume')
    # Get parameters for strategy function
    args = data_cfg['strategy_instance']['args_params']
    kwargs = data_cfg['strategy_instance']['kwargs_params']

    # The bot start to run
    try:

        for s, p, v in SM(*args.copy(), **kwargs.copy()):
            logger.info('{}th iteration'.format(SM.t))
            logger.info('Signal is {}, price is {} and IV is {}'.format(
                s, p, v
            ))

            # Set order
            outputs = OM.set_order(s, price=p, volume=vol * v, **order_params)

            # Check to verify and debug
            if not order_params['validate']:

                for output in outputs:
                    id_order = output['result']['userref']
                    status = OM.get_status_order(id_order)
                    logger.info(check(status))

            # TODO : save new volume to invest if reinvest
            # TODO : compute, print and save some statistics
            # Clean outputs
            outputs = set_order_results(outputs)
            # txt = 'Check output:\n'
            # txt += check(*outputs)
            # logger.debug(txt)

            # Update result historic
            update_result_hist(outputs, '', path=path + id_strat)

            # Update order historic
            update_order_hist(outputs, '', path=path + id_strat)

            # TODO : Print results
            print_results(outputs)

            # Get current pos
            current_pos = float(OM.current_pos)
            current_vol = float(OM.current_vol)
            logger.info('Current position: {:.2f}'.format(current_pos))
            logger.info('Current volume: {:.2f}\n\n'.format(current_vol))
            # TODO : check if current position is ok
            data_cfg['pre_order_instance']['current_pos'] = float(current_pos)
            # TODO : check if current volume is ok
            data_cfg['pre_order_instance']['current_vol'] = float(current_vol)
            pass

        else:
            print('\nAll is good\n')

    except Exception as error:

        # TODO : how manage unknown error
        # Report error TODO : Improve
        # time_str = time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(now()))
        # txt = '\nUNKNOWN ERROR\n'
        # txt += 'In {} script '.format(sys.argv[0])
        # txt += 'for {} strat id '.format(id_strat)
        # txt += 'at {} UTC, '.format(time_str)
        # txt += 'the following error occurs:\n'
        # txt += '{}: {}\n'.format(str(type(error)), str(error))
        # print(txt)
        logger.error(
            'UNKNOWN ERROR: {}\n{}'.format(str(type(error)), str(error)),
            exc_info=True
        )

        # with open(path + id_strat + '/errors.log', 'a') as f:
        #    f.write(txt)
        raise error

    finally:
        # DEGUG
        df_ord = get_df(path + id_strat, 'orders_hist', '.dat')
        df_res = get_df(path + id_strat, 'result_hist', '.dat')
        print('Historic:\n' + '-' * 9 + '\n')
        print(df_ord.tail(), '\n')
        print(df_res.tail(), '\n')
        # TODO : ending with save some statistics and others
        # TODO : save new volume to invest if reinvest
        # Reset poped parameters
        order_params['volume'] = vol
        # Save current position and volume
        dump_config_params(data_cfg, path + id_strat + '/configuration.yaml')
        logger.info('Bot stopped. See you soon !')


if __name__ == '__main__':

    import logging.config
    import yaml

    with open('./strategy_manager/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)
    txt = '\nStrategy {} starts to run !\n'.format(sys.argv[1])
    txt += '-' * (len(txt) - 2) + '\n'
    print(txt)
    run_bot(sys.argv[1])
