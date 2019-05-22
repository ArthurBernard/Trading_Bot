#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-05-19 13:05:40
# @Last modified by: ArthurBernard
# @Last modified time: 2019-05-22 08:49:03

""" Read and print dataframe results of a strategy bot. """

# Built-in packages
import logging
import sys

# External packages

# Local packages
from results_manager import ResultManager
from tools.utils import load_config_params


def print_results(id_strat, n_last_results=None, path='strategies/',
                  metrics=[], periods=[]):
    """ Display statistics of a bot results for a specified configuration.

    Parameters
    ----------
    strat_id : str
        A strat id is the name of the corresonding configuration file.
    n_last_results : int, optional
        Number of lines of dataframe results to display. Default is `None`.
    path : str, optional
        Path to load the configuration file.
    metrics : list of str, optional
        List of metrics to display results. Default is `None` and display
        metrics in configuration file.
    periods : list of str, optional
        List of periods to display results. Default is `None` and display
        periods in configuration file.

    """
    logger = logging.getLogger('strat_man.' + __name__)

    if path[-1] != '/':
        path += '/'

    # Load data configuration
    logger.info('Load parameters of {}'.format(id_strat))
    data_cfg = load_config_params(path + id_strat + '/configuration.yaml')

    if metrics:
        data_cfg['result_instance']['metrics'] = metrics

    if periods:
        data_cfg['result_instance']['periods'] = periods

    # Set result manager configuration
    RM = ResultManager(**data_cfg['result_instance'])

    # Print some statistics
    RM.print_stats()

    if n_last_results is not None:
        logger.info('Historic result:\n\n' + str(RM.df.tail(n_last_results)))


if __name__ == '__main__':

    import logging.config
    import yaml

    with open('./strategy_manager/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    args = sys.argv[1:].copy()
    id_strat = args.pop(0)

    i = 0
    metrics, periods = [], []
    for arg in sys.argv[2:]:
        if arg.lower() in ['return', 'perf', 'sharpe', 'calmar', 'maxdd']:
            metrics += [args.pop(i)]
        elif arg.lower() in ['daily', 'weekly', 'monthly', 'yearly', 'total']:
            periods += [args.pop(i)]
        else:
            i += 1

    if not args:
        n_last_results = None
    elif len(args) == 1:
        n_last_results = int(args[0])
    else:
        raise ValueError('Unkown parameters : {}'.format(args))

    print_results(id_strat, n_last_results=n_last_results, metrics=metrics,
                  periods=periods)
