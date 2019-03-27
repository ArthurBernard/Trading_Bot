#!/usr/bin/env python3
# coding: utf-8

# Built-in packages
import time

# External packages
import pandas as pd

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
            'price': float(list_ord[5]),
            'leverage': 1 if len(list_ord) == 6 else list_ord[7][0],
        })
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
    now = time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time()))
    txt = ''
    txt += '\nAt {}: {}\n'.format(now, str(out))
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
    print(df_hist.head())

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
    # Get order historic dataframe
    df_hist = get_df(path, name + '_ord_hist', '.dat')
    # Set new order historic dataframe
    df_hist = df_hist.append(set_order_hist(order_result))
    df_hist = df_hist.reset_index(drop=True)
    # Save order historic dataframe
    save_df(df_hist, path, name + '_ord_hist', '.dat')
