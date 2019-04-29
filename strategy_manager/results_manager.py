#!/usr/bin/env python3
# coding: utf-8

# Built-in packages
import time

# External packages
import pandas as pd
import numpy as np

# Internal packages
from strategy_manager.tools.utils import get_df, save_df

__all__ = [
    'set_order_result', 'set_order_results', 'set_order_hist',
    'update_order_hist',
]

"""
TODO:
    - Print stats about strategy
    - Profit and loss histo
    - Print profit and loss
    - Plot strategy graph vs underlying
    - Extract order historic (to allow statistic by date, pair, all, etc.)

"""


def set_order_result(order_result):
    """ Clean the output of set order method.

    Parameters
    ----------
    order_result : dict
        Output of set order.

    Returns
    -------
    order_result : dict
        Cleaned result of an output order.

    """
    descr = order_result.pop('descr')

    if descr is not None:
        list_ord = descr['order'].split(' ')
        order_result.update({
            'type': list_ord[0],
            'volume': float(list_ord[1]),
            'pair': list_ord[2],
            'ordertype': list_ord[4],
        })

        if order_result['ordertype'] == 'limit':
            order_result.update({
                'price': float(list_ord[5]),
                'leverage': 1 if len(list_ord) == 6 else list_ord[7][0],
            })

        elif order_result['ordertype'] == 'market':
            # TODO : /!\ get execution price for market order /!\
            order_result.update({
                'price': float(list_ord[5]),  # request execution price
                'leverage': 1 if len(list_ord) == 5 else list_ord[6][0],
            })

        else:
            raise ValueError('Unknown order type: {}'.format(list_ord[4]))

        return order_result

    else:

        return order_result


def set_order_results(order_results):
    """ Clean the output of set orders method.

    Parameters
    ----------
    order_results : list of dict
        Output of set order.

    Returns
    -------
    clean_order_results : list of dict
        Cleaned results of output orders.

    """
    clean_order_result = []

    for result in order_results:
        clean_order_result += [set_order_result(result['result'])]

    else:

        return clean_order_result


def print_results(out):
    now_ts = time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time()))
    txt = '\nAt {}:\n\n'.format(now_ts)
    i = 1

    for o in out:
        txt += '{}th order: {}\n\n'.format(i, o)
        i += 1

    print(txt)


def set_statistic():
    # TODO : set stats, profit and loss, etc
    pass


def set_order_hist(order_result):
    """ Set dataframe of historic order.

    Parameters
    ----------
    order_result : dict or list of dict
        Cleaned result of one or several output order.

    Returns
    -------
    df_hist : pandas.DataFrame
        Order result as dataframe.

    """

    df_hist = pd.DataFrame(order_result, columns=[
        'timestamp', 'txid', 'userref', 'price', 'volume',
        'type', 'pair', 'ordertype', 'leverage'
    ])

    return df_hist


def update_order_hist(order_result, name, path='.'):
    """ Update the historic order dataframe.

    Parameters
    ----------
    order_result : dict or list of dict
        Cleaned result of one or several output order.

    """
    # TODO : Save by year ? month ? day ?
    # TODO : Don't save per strategy ?
    if path[-1] != '/':
        path += '/'

    # Get order historic dataframe
    df_hist = get_df(path, name + 'orders_hist', '.dat')

    # Set new order historic dataframe
    df_hist = df_hist.append(set_order_hist(order_result), sort=False)
    df_hist = df_hist.reset_index(drop=True)

    # Save order historic dataframe
    save_df(df_hist, path, name + 'orders_hist', '.dat')


def set_results(order_results):
    """ Aggregate and set results.

    Parameters
    ----------
    order_result : list of dict
        Cleaned result of one or several output order.

    Returns
    -------
    aggr_res : pd.DataFrame
        Strategy result as dataframe.

    """
    aggr_res = {}

    for result in order_results:
        ts = result['timestamp']

        if ts not in aggr_res.keys():
            aggr_res[ts] = {'volume': 0, 'position': 0, 'price': 0, 'fee': 0}

        aggr_res[ts]['volume'] += result['current_volume']
        aggr_res[ts]['position'] += result['current_position']
        aggr_res[ts]['price'] = result['price']
        aggr_res[ts]['fee'] = result['fee']

    else:

        return pd.DataFrame(aggr_res).T


def get_result_hist(name, path='.'):
    """ Load result historic strategy.

    Parameters
    ----------
    path, name : str
        Path and name of the file to load.

    Returns
    -------
    df : pandas.DataFrame
        A dataframe of results strategy.

    """
    df = get_df(path, name + 'result_hist', '.dat')

    if df.empty:

        return pd.DataFrame(columns=['price', 'volume', 'position', 'fee'])

    else:

        return df


def update_result_hist(order_results, name, path='.', fee=0.0016):
    """ Load, merge and save result historic strategy.

    Parameters
    ----------
    order_results : list of dict
        Cleaned result of one or several output order.
    path, name : str
        Path and name of the file to load.

    """
    if path[-1] != '/':
        path += '/'

    # Get result historic
    hist = get_result_hist(name, path=path)
    df = set_results(order_results)

    # Merge result historics
    hist = hist.append(df, sort=False)
    idx = hist.index

    # if idx.size <= 1:
    #    hist.loc[idx[0], 'return'] = 0

    if False:
        p = hist.loc[:, 'price'].values
        vol = hist.loc[:, 'volume'].values
        pos = hist.loc[:, 'position'].values
        fees = fee * (pos[:-1] - pos[1:])
        ret = (p[1:] - p[:-1]) * vol[:-1] * pos[:-1]
        hist.loc[idx[1]:, 'return_raw'] = ret
        hist.loc[idx[1]:, 'return_net'] = ret * (1 - fees)
        hist.loc[idx[1]:, 'cum_return'] = np.cumsum(ret * (1 - fees))
        # hist.loc[idx[1]:, '']

    # Save order historic dataframe
    save_df(hist, path, name + 'result_hist', '.dat')


def print_stats(name, path='.', volume=1.):
    """ Print some statistics of result historic strategy.

    Parameters
    ----------
    path, name : str
        Path and name of the file to load.

    """
    df = get_result_hist(name, path=path)
    last_ts = df.index[-1]
    ui_perf, strat_perf = set_stats(df.loc[df.index >= last_ts - 86400])
    txt = '\nUnderlying perf : {}\n'.format(ui_perf)
    txt += 'Strategy perf : {}\n'.format(strat_perf)
    print(txt)


def set_stats(df):
    p = df.loc[:, 'price'].values
    vol = df.loc[:, 'volume'].values
    pos = df.loc[:, 'position'].values
    fee = df.loc[:, 'fee'].values
    fees = fee[:-1] * (pos[:-1] - pos[1:])
    ret = (p[1:] - p[:-1]) * vol[:-1] * pos[:-1]

    return p[0] - p[-1], np.sum(ret * (1 - fees))
    # hist.loc[idx[1]:, 'return_raw'] = ret
    # hist.loc[idx[1]:, 'return_net'] = ret * (1 - fees)
    # hist.loc[idx[1]:, 'cum_return'] = np.cumsum(ret * (1 - fees))
