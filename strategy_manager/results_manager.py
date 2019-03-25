#!/usr/bin/env python3
# coding: utf-8

# Built-in packages
import time

# External packages

# Internal packages

__all__ = ['print_results']

"""
TODO:
    - Function to save historic (signal, prices, volume, time interval, ?)
    - Print stats about strategy
    - Profit and loss
    - Plot strategy graph vs underlying

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
    list_ord = order_result.pop('descr')['order'].split(' ')

    order_result = {
        'type': list_ord[0],
        'volume': list_ord[1],
        'pair': list_ord[2],
        'ordertype': list_ord[4],
        'price': list_ord[5],
        'leverage': 1 if len(list_ord) == 6 else list_ord[7][0],
        **order_result
    }

    return order_result


def print_results(out):
    now = time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time()))
    txt = ''
    txt += '\nAt {}: {}\n'.format(now, str(out))
    print(txt)


def set_statistic():
    # TODO : set stats, profit and loss, etc
    pass


def get_historic(path):
    try:
        # TODO : load data
        pass
    except FileNotFoundError:
        # TODO : set data file
        pass


def set_historic(path):
    pass
