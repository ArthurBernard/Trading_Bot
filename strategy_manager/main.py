#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-05-03 17:36:22
# @Last modified by: ArthurBernard
# @Last modified time: 2019-09-04 08:45:20

""" Run a bot following a configuration file. """

# Built-in packages
import sys
import logging

# Local packages
from manager import StrategyManager
from tools.utils import load_config_params, dump_config_params, get_df
from orders_manager import SetOrder
from results_manager import update_order_hist, ResultManager

__all__ = ['run_bot']


def run_bot(id_strat, path='./strategies/', STOP=None):
    """ Run a strategy bot for specified configuration file.

    Parameters
    ----------
    strat_id : str
        A strat id is the name of the corresonding configuration file.
    path : str
        Path to load the configuration file.

    """
    logger = logging.getLogger('strat_man.' + __name__)

    if path[-1] != '/':
        path += '/'

    # Load data configuration
    logger.info('Load parameters of {}'.format(id_strat))
    data_cfg = load_config_params(path + id_strat + '/configuration.yaml')

    # Set strategy manager and data request configuration
    SM = StrategyManager(**data_cfg['strat_manager_instance'].copy())

    if STOP is not None:
        SM.STOP = STOP

    SM.set_data_manager(**data_cfg['get_data_instance'].copy())

    # Set pre order configuration
    OM = SetOrder(**data_cfg['pre_order_instance'])

    # Get parameters for strategy function
    args = data_cfg['strategy_instance']['args_params']
    kwargs = data_cfg['strategy_instance']['kwargs_params']
    order_params = data_cfg['order_instance']

    # Set result manager configuration
    RM = ResultManager(**data_cfg['result_instance'])

    # The bot start to run
    try:
        for signal, add_params in SM(*args.copy(), **kwargs.copy()):
            logger.info('Signal is {}.'.format(signal))
            logger.debug('Additional parameters are {}\n'.format(add_params))
            logger.debug('Order parameters are {}\n'.format(order_params))

            # Set order
            outputs = OM.set_order(signal, **order_params, **add_params)

            # Update order historic
            update_order_hist(outputs, '', path=path + id_strat)

            # Update result historic
            RM.update_result_hist(outputs)
            # Print some statistics
            RM.print_stats()
            # TODO : reinvest profit option
            if data_cfg['result_instance']['reinvest_profit']:
                new_volume = RM.get_current_volume()
                data_cfg['order_instance']['volume'] = round(new_volume, 8)

            # Get current pos
            current_pos = float(OM.current_pos)
            current_vol = float(OM.current_vol)
            logger.info('Current position: {:.2f}'.format(current_pos))
            logger.info('Current volume: {:.2f}\n\n'.format(current_vol))
            # TODO : check if current position is ok
            data_cfg['pre_order_instance']['current_pos'] = float(current_pos)
            # TODO : check if current volume is ok
            data_cfg['pre_order_instance']['current_vol'] = float(current_vol)

        logger.debug('All is good\n')

    except Exception as e:

        # TODO : how manage unknown error
        # Report error TODO : Improve
        logger.error('Unkownn error: {}'.format(type(e)), exc_info=True)

        raise e

    finally:
        # DEGUG
        df_ord = get_df(path + id_strat, 'orders_hist', '.dat')
        logger.info('Historic orders:\n' + str(df_ord.iloc[:, 1:].tail()))
        logger.info('Historic result:\n' + str(RM.df.tail()))
        # Save results
        RM.save_result_hist()
        # TODO : ending with save some statistics and others
        # TODO : save new volume to invest if reinvest
        # Save current position and volume
        logger.info('Save parameters of {}'.format(id_strat))
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

    try:
        STOP = int(sys.argv[-1])

    except ValueError:
        STOP = None

    run_bot(sys.argv[1], STOP=STOP)
