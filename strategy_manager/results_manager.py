#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-05-02 19:07:38
# @Last modified by: ArthurBernard
# @Last modified time: 2019-05-11 14:11:27

""" Tools to manager results and display it. """

# Built-in packages
import logging

# External packages
import pandas as pd
import numpy as np
import fynance as fy

# Local packages
from strategy_manager.tools.utils import get_df, save_df

__all__ = [
]

"""
TODO:
    - Print stats about strategy
    - Profit and loss histo
    - Print profit and loss
    - Plot strategy graph vs underlying
    - Extract order historic (to allow statistic by date, pair, all, etc.)

"""


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


class ResultManager:
    """ Print some statistics of result historic strategy.

    Parameters
    ----------
    path : str
        Path of the file to load and save results.
    init_vol : float, optional
        Initial value invested to the strategy.
    period : int, optional
        Number of period per year, default is 252 (trading days).
    metrics : list of str
        List of metrics to display results. Is available 'return', 'perf',
        'sharpe', 'calmar' and 'maxdd'.
    periods : list of str
        List of periods to compte metrics. Is available 'daily', 'weekly',
        'monthly', 'yearly' and 'total'

    """

    def __init__(self, path, init_vol=1., period=252, metrics=[], periods=[]):
        if path[-1] != '/':
            path += '/'

        self.path = path
        self.init_vol = init_vol
        self.period = period
        self.metrics = metrics
        self.periods = periods
        self.df = get_df(path, 'result_hist', ext='.dat')
        self.logger = logging.getLogger('strat_man.' + __name__)

    def update_result_hist(self, order_results):
        """ Load, merge and save result historic strategy.

        Parameters
        ----------
        order_results : list of dict
            Cleaned result of one or several output order.

        """
        df = set_results(order_results)

        if self.df.empty:
            df.loc[:, 'value'] = self.init_vol

        else:
            df = self.df.iloc[-1:].append(df, sort=False)
            df = df.fillna(method='ffill')
            df.loc[:, 'value'] += set_performance(df)

        self.df = self.df.iloc[:-1].append(df, sort=False)

        return self

    def save_result_hist(self):
        """ Save result historic. """
        save_df(self.df, self.path, 'result_hist', ext='.dat')

    def print_stats(self):
        """ Print some statistics of result historic strategy. """
        price = self.df.price.iloc[-1]
        value = self.df.value.iloc[-1]
        pos = self.df.position.iloc[-1]
        # TODO : fix problem with vol equal to 0. when pos is 0
        vol = self.df.volume.iloc[-1]
        txt = 'Display results\n' + set_text(
            ['-'],
            ['Price of the underlying: {:.2f}'.format(price)],
            ['Current fees: {:.2}%'.format(self.df.fee.iloc[-1])],
            ['-'],
        )

        txt += '\nCurrent value of the porfolio:\n'
        txt += set_text(['-'] * 3, [
            'Portfolio',
            '{:.2f} $'.format(value),
            '{:.2f} ?'.format(value / price), ], [
            'Base part.',
            '{:.2f} $'.format(pos * vol * price),
            '{:.2%}'.format(pos), ], [
            'Underlying part',
            '{:.2f} $'.format((1 - pos) * vol * price),
            '{:.2%}'.format(1 - pos), ], ['-'] * 3)

        txt_table = [['-'] * (1 + len(self.metrics)), ['   '] + self.metrics]

        for period in self.periods:
            if period.lower() == 'daily':
                _index = self.df.index >= self.df.index[-1] - 86400

            elif period.lower() == 'weekly':
                _index = self.df.index >= self.df.index[-1] - 86400 * 7

            elif period.lower() == 'monthly':
                _index = self.df.index >= self.df.index[-1] - 86400 * 30

            elif period.lower() == 'yearly':
                _index = self.df.index >= self.df.index[-1] - 86400 * 365

            elif period.lower() == 'total':
                _index = self.df.index >= self.df.index[0]

            else:
                self.logger.error('Unknown period: {}'.format(period))
                continue

            txt_table += self.set_stats_result(self.df.loc[_index], period)

        txt_table += (['-'] * (1 + len(self.metrics)),)

        txt += '\nStatistics of results:\n' + set_text(*txt_table)

        self.logger.info(txt)

    def set_stats_result(self, df, head):
        """ Compute stats `backward` seconds in past. """
        ui = df.price.values
        si = df.value.values

        return (
            ['-'] * (1 + len(self.metrics)),
            [head],
            ['Underlying'] + self.set_statistics(ui),
            ['Strategy'] + self.set_statistics(si),
        )

    def set_statistics(self, series):
        metric_values = []
        for metric in self.metrics:
            if metric.lower() == 'return':
                metric_values += [series[-1] - series[0]]

            elif metric.lower() in ['perf', 'perf.', 'performance']:
                metric_values += [series[-1] / series[0] - 1.]

            elif metric.lower() == 'sharpe':
                metric_values += [fy.sharpe(series, period=self.period)]

            elif metric.lower() == 'calmar':
                metric_values += [fy.calmar(series, period=self.period)]

            elif metric.lower() == 'maxdd':
                metric_values += [fy.mdd(series)]

            else:
                self.logger.error('Unknown metric: {}'.format(metric))
                continue

        return rounder(*metric_values, dec=2)


def set_text(*args):
    n = max(len(arg) for arg in args)
    k_list = ['| ' if len(arg[0]) > 1 else '+' for arg in args]

    for i in range(n):
        i_args, n_spa, j = [], 0, 0

        for arg in args:
            if len(arg) >= i + 1:
                i_args += [arg]
                n_spa = max(n_spa, len(str(arg[i])))

        for arg in args:
            if len(arg[0]) > 1 and len(arg) >= i + 1:
                space = ' ' * (n_spa - len(str(arg[i])))
                k_list[j] += str(arg[i]) + space + ' | '

            elif len(arg[0]) == 1 and len(arg) >= i + 1:
                k_list[j] += arg[i] * (n_spa + 2) + '+'

            else:
                if i % 2 == 0:
                    k_list[j] = k_list[j][:-2] + ' ' * (n_spa + 3) + '| '

                else:
                    k_list[j] = '|' + ' ' * (n_spa + 3) + k_list[j][1:]

            j += 1

    return '\n'.join(k_list)


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


def set_performance(df):
    """ Compute performance of a strategy. """
    p = df.loc[:, 'price'].values
    ret = np.zeros([p.size])
    vol = df.loc[:, 'volume'].values
    pos = df.loc[:, 'position'].values
    fee = df.loc[:, 'fee'].values
    fees = fee[:-1] * (pos[:-1] - pos[1:])
    ret[1:] = (p[1:] - p[:-1]) * vol[:-1] * pos[:-1] * (1 - fees)

    return np.cumsum(ret)


def rounder(*args, dec=0):
    return [round(arg, dec) for arg in args]
