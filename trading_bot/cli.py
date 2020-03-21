#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-03-17 12:23:25
# @Last modified by: ArthurBernard
# @Last modified time: 2020-03-21 19:59:12

""" A (very) light Graphical User Interface. """

# Built-in packages
import logging
from pickle import Unpickler
from threading import Thread

# Third party packages
import fynance as fy
import numpy as np
import pandas as pd

# Local packages
from trading_bot._client import _ClientCLI
from trading_bot.performance import PnL
from trading_bot.tools.io import load_config_params


class ResultManager:
    """ Manager object of historical results of strategy.

    Attributes
    ----------
    df : pandas.DataFrame
        Data with each series to compute performances.
    period : int
        Maximal number of trading periods per year
    metrics : list of str
        List of metrics to compute performance. The following are available:
        'return', 'perf', 'sharpe', 'calmar', and 'maxdd'.
    periods : list of str
        Frequency to compute performances. The following are available:
        'daily', 'weekly', 'monthly', 'yearly' and 'total'.

    Methods
    -------
    print_stats
    set_current_price
    set_current_value
    set_current_stats

    """

    def __init__(self, pnl, metrics=[], periods=[], period=252):
        """ Initialize object.

        Parameters
        ----------
        pnl : PnL object or pd.DataFrame
            Object to compute and store profit and loss of a trading bot.
        metrics : list of str
            List of metrics to display results. Is available 'return', 'perf',
            'sharpe', 'calmar' and 'maxdd'.
        periods : list of str
            List of periods to compte metrics. Is available 'daily', 'weekly',
            'monthly', 'yearly' and 'total'.
        period : int, optional
            Number of trading days per year, i.e 252 if trading on classical
            market and 364 for crypto-currencies market. Default is 252.

        """
        if isinstance(pnl, PnL):
            self.df = pnl.df
            self.period = period * 86400 / pnl.ts

        elif isinstance(pnl, pd.DataFrame):
            self.df = pnl
            self.period = period

        self.metrics = metrics
        self.periods = periods
        self.df = pnl.df
        self.logger = logging.getLogger(__name__)

    def set_current_price(self):
        """ Display current price and fees. """
        txt = 'Display results\n' + _set_text(
            ['-'],
            ['Price of the underlying: {:.2f}'.format(self.df.price.iloc[-1])],
            ['Current fees: {:.2}%'.format(self._get_current_fee())],
            ['-'],
        )

        return txt

    def set_current_value(self):
        """ Display the current share of portfolio in underlying and cash. """
        price = self.df.price.iloc[-1]
        value = self.df.value.iloc[-1]
        pos = self.df.position.iloc[-1]
        # TODO : fix problem with volume equal to 0. when pos is 0
        vol = self.df.volume.iloc[-1]
        txt = '\nCurrent value of the porfolio:\n'
        txt += _set_text(['-'] * 3, [
            'Portfolio',
            '{:.2f} $'.format(value),
            '{:.2f} ?'.format(value / price), ], [
            'Underlying part.',
            '{:.2f} $'.format(pos * vol * price),
            '{:.2%}'.format(pos), ], [
            'Base part',
            '{:.2f} $'.format((1 - pos) * vol * price),
            '{:.2%}'.format(1 - pos), ], ['-'] * 3)

        return txt

    def set_current_stats(self):
        """ Display some statistics for some time periods. """
        txt_table = [['-'] * (1 + len(self.metrics)), ['   '] + self.metrics]

        for period in self.periods:
            _index = self._get_period_index(period)
            if _index is None:
                continue

            txt_table += self._set_stats_result(self.df.loc[_index], period)

        txt_table += (['-'] * (1 + len(self.metrics)),)

        return '\nStatistics of results:\n' + _set_text(*txt_table)

    def print_stats(self):
        """ Print some statistics of historical results strategy. """
        txt = self.set_current_price()
        txt += self.set_current_value()
        txt += self.set_current_stats()
        # self.logger.info(txt)

        return txt

    def _set_stats_result2(self, df, head, col=None):
        """ Set statistics in a table with header. """
        table = [['-'] * (1 + len(self.metrics)), [head]]
        col = ['price', 'value'] if col is None else col
        for c in col:
            table += [[str(c)] + self.set_statistics(df.loc[:, c].values)]

        return table

    def _set_stats_result(self, df, head):
        """ Set statistics in a table with header. """
        ui = df.price.values
        si = df.value.values

        return [
            ['-'] * (1 + len(self.metrics)),
            [head],
            ['Underlying'] + self.set_statistics(ui),
            ['Strategy'] + self.set_statistics(si),
        ]

    def set_statistics(self, series):
        """ Compute statistics of a series of price or index values.

        Parameters
        ----------
        series : np.ndarray[ndim=1, dtype=np.float64]
            Series of price or index values.

        Returns
        -------
        list
            Some statistics predefined when initialize the object.

        """
        metric_values = []
        for metric in self.metrics:
            if series.size < 2:
                metric_values += [0]

            elif metric.lower() == 'return':
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

        return _rounder(*metric_values, dec=2)

    def _get_current_fee(self):
        fee = self.df.loc[self.df.fee != 0., 'fee'].values
        val = self.df.value.values
        if not fee.size:

            return 0.

        return 100 * fee[-1] / val[-1]

    def _get_period_index(self, period):
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
            _index = None

        return _index


def _set_text(*args):
    """ Set a table. """
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


def _rounder(*args, dec=0):
    """ Round each element of a list. """
    return [round(float(arg), dec) for arg in args]


class _ResultManager(ResultManager):
    """ Manager object of historical results of strategy.

    Attributes
    ----------
    df : pandas.DataFrame
        Data with each series to compute performances.
    period : int
        Maximal number of trading periods per year
    metrics : list of str
        List of metrics to compute performance. The following are available:
        'return', 'perf', 'sharpe', 'calmar', and 'maxdd'.
    periods : list of str
        Frequency to compute performances. The following are available:
        'daily', 'weekly', 'monthly', 'yearly' and 'total'.

    Methods
    -------
    print_stats
    set_current_price
    set_current_value
    set_current_stats

    """

    def __init__(self, pnl_dict, period=252):  # , metrics=[], periods=[]):
        """ Initialize object.

        Parameters
        ----------
        pnl : dict of pd.DataFrame
            Object to compute and store profit and loss of a trading bot.
        metrics : list of str
            List of metrics to display results. Is available 'return', 'perf',
            'sharpe', 'calmar' and 'maxdd'.
        periods : list of str
            List of periods to compte metrics. Is available 'daily', 'weekly',
            'monthly', 'yearly' and 'total'.
        period : int, optional
            Number of trading days per year, i.e 252 if trading on classical
            market and 364 for crypto-currencies market. Default is 252.

        """
        self.pnl = pnl_dict
        for key, value in pnl_dict.items():
            idx = value['pnl'].index
            ts = (idx[1:] - idx[:-1]).min()
            self.pnl[key]['period'] = period * 86400 / ts

        self.metrics = ['return', 'perf', 'sharpe', 'calmar', 'maxdd']  # metrics
        self.periods = ['daily', 'weekly', 'monthly', 'yearly', 'total']  # periods
        self.logger = logging.getLogger(__name__)

    # def set_current_price(self):
    #    """ Display current price and fees. """
    #    txt = 'Display results\n' + _set_text(
    #        ['-'],
    #        ['Price of the underlying: {:.2f}'.format(self.df.price.iloc[-1])],
    #        ['Current fees: {:.2}%'.format(self._get_current_fee())],
    #        ['-'],
    #    )

    #    return txt

    # def set_current_value(self):
    #    """ Display the current share of portfolio in underlying and cash. """
    #    price = self.df.price.iloc[-1]
    #    value = self.df.value.iloc[-1]
    #    pos = self.df.position.iloc[-1]
    #    # TODO : fix problem with volume equal to 0. when pos is 0
    #    vol = self.df.volume.iloc[-1]
    #    txt = '\nCurrent value of the porfolio:\n'
    #    txt += _set_text(['-'] * 3, [
    #        'Portfolio',
    #        '{:.2f} $'.format(value),
    #        '{:.2f} ?'.format(value / price), ], [
    #        'Underlying part.',
    #        '{:.2f} $'.format(pos * vol * price),
    #        '{:.2%}'.format(pos), ], [
    #        'Base part',
    #        '{:.2f} $'.format((1 - pos) * vol * price),
    #        '{:.2%}'.format(1 - pos), ], ['-'] * 3)

    #    return txt

    def get_current_stats(self):
        """ Display some statistics for some time periods. """
        txt_table = [['-'] * (1 + len(self.metrics)), ['   '] + self.metrics]

        for period in self.periods:
            txt_table += [['-'] * (1 + len(self.metrics)), [period]]
            for key, value in self.pnl.items():
                df = value['pnl']
                _index = self._get_period_index(df, period)
                if _index is None:

                    continue

                txt_table += self._set_stats_result2(
                    df.loc[_index],
                    period,
                    value['period'],
                    col={'price': value['pair'], 'value': key}
                    # sn=key,
                )

        txt_table += (['-'] * (1 + len(self.metrics)),)

        return '\nStatistics of results:\n' + _set_text(*txt_table)

    def _set_stats_result2(self, df, head, period, col=None):
        """ Set statistics in a table with header. """
        # table = [['-'] * (1 + len(self.metrics)), [head]]
        table = []
        col = {'price': 'underlying', 'value': 'strategy'} if col is None else col
        for k, a in col.items():
            table += [[str(a)] + self.set_statistics(df.loc[:, k].values, period)]

        return table

    def _set_stats_result(self, df, head, period, un='Underlying', sn='Strategy'):
        """ Set statistics in a table with header. """
        ui = df.price.values
        si = df.value.values

        return [
            ['-'] * (1 + len(self.metrics)),
            [head],
            [un] + self.set_statistics(ui, period),
            [sn] + self.set_statistics(si, period),
        ]

    def set_statistics(self, series, period):
        """ Compute statistics of a series of price or index values.

        Parameters
        ----------
        series : np.ndarray[ndim=1, dtype=np.float64]
            Series of price or index values.

        Returns
        -------
        list
            Some statistics predefined when initialize the object.

        """
        metric_values = []
        for metric in self.metrics:
            if series.size < 2:
                metric_values += [0]

            elif metric.lower() == 'return':
                metric_values += [series[-1] - series[0]]

            elif metric.lower() in ['perf', 'perf.', 'performance']:
                metric_values += [series[-1] / series[0] - 1.]

            elif metric.lower() == 'sharpe':
                metric_values += [fy.sharpe(series, period=period)]

            elif metric.lower() == 'calmar':
                metric_values += [fy.calmar(series, period=period)]

            elif metric.lower() == 'maxdd':
                metric_values += [fy.mdd(series)]

            else:
                self.logger.error('Unknown metric: {}'.format(metric))

        return _rounder(*metric_values, dec=2)

    def _get_period_index(self, df, period):
        if period.lower() == 'daily':
            _index = df.index >= df.index[-1] - 86400

        elif period.lower() == 'weekly':
            _index = df.index >= df.index[-1] - 86400 * 7

        elif period.lower() == 'monthly':
            _index = df.index >= df.index[-1] - 86400 * 30

        elif period.lower() == 'yearly':
            _index = df.index >= df.index[-1] - 86400 * 365

        elif period.lower() == 'total':
            _index = df.index >= df.index[0]

        else:
            self.logger.error('Unknown period: {}'.format(period))
            _index = None

        return _index


class CLI(_ClientCLI):
    """ Object to allow a Command Line Interface. """

    txt = 'press any key to update data or press q to quit'
    strat_bot = {}
    pair = {}
    # index = pd.Index([], np.int64)

    def __init__(self, path, address=('', 50000), authkey=b'tradingbot'):
        super(CLI, self).__init__(address=address, authkey=authkey)
        self.logger = logging.getLogger(__name__)
        self.path = path

    def __enter__(self):
        """ Enter. """
        self.logger.debug('enter')
        # TODO : Load config ?
        super(CLI, self).__enter__()
        self.conn_tbm.thread = Thread(target=self.listen_tbm, daemon=True)
        self.conn_tbm.thread.start()

        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        """ Exit. """
        # TODO : Save configuration ?
        if exc_type is not None:
            self.logger.error(
                '{}: {}'.format(exc_type, exc_value),
                exc_info=True
            )

        super(CLI, self).__exit__(exc_type, exc_value, exc_tb)
        self.conn_tbm.thread.join()
        self.logger.debug('exit')

    def __iter__(self):
        return self

    def __next__(self):
        if self.is_stop():

            raise StopIteration

        k = input(self.txt)
        k = k if len(k) > 0 else ' '
        if k == 'q':

            raise StopIteration

        elif k[0] == 'f':
            # todo : ask current fees
            pass

        elif k[0] == 'b':
            # todo : ask current balance
            pass

        else:

            return 'sb_update'

    def display(self):
        self.logger.debug('display')
        rm = _ResultManager(self.strat_bot)
        print(rm.get_current_stats())
        return None
        for pair, list_name in self.pair.items():
            older_hist = {'name': None, 'available': 0}
            for name in list_pair:
                pnl = self.strat_bot[name]['pnl']
                rm = _ResultManager(pnl, **self.strat_bot[name]['kwrd'])

                availabe = pnl.index[-1] - pnl.index[0]
                if available > older_hist['availabe']:
                    older_hist['availabe'] = available
                    older_hist['name'] = name
                strat_value = pnl.value.values
                # todo : compute perf for strat

            pair_value = self.strat_bot[older_hist['name']]['pnl'].price.values
            # todo : compute perf for pair underlying

        # todo display performance table
        pass

    def listen_tbm(self):
        self.logger.debug('start listen TradingBotManager')
        for k, a in self.conn_tbm:
            if k is None:

                continue

            self._handler_tbm(k, a)
            self.update()
            # TODO : display performances
            self.display()
            if self.is_stop():
                self.conn_tbm.shutdown()

        self.logger.debug('stop listen TradingBotManager')

    def run(self):
        for k in self:
            if k == 'sb_update':
                self.conn_tbm.send((k, None),)

    def update(self):
        self.logger.debug('update start')
        for k in self.strat_bot:
            txt = 'update {}'.format(k)
            with open(self.path + k + '/PnL.dat', 'rb') as f:
                pnl = Unpickler(f).load()

            # self.index = self.index.append(pnl.index).drop_duplicates()
            value = pnl.value.iloc[-1]
            txt += ' | value is {:.2f}'.format(value)
            txt += ' and volume is {:.8f}'.format(value / pnl.price.iloc[-1])
            self.logger.debug(txt)
            self.strat_bot[k]['pnl'] = pnl

    def _handler_tbm(self, k, a):
        if k is None:
            pass

        elif k == 'sb_update':
            self.pair = {}
            # self.index = pd.Index([], np.int64)
            self.strat_bot = {n: self._get_sb_dict(i, n) for i, n in a.items()}

        else:
            self.logger.error('received unknown message {}: {}'.format(k, a))

    def _get_sb_dict(self, _id, name):
        # load some configuration info
        cfg = load_config_params(self.path + name + '/configuration.yaml')
        pair = cfg['order_instance']['pair']
        freq = cfg['strat_manager_instance']['frequency']
        kwrd = cfg['result_instance']
        if pair not in self.pair:
            self.pair[pair] = []

        self.pair[pair] += [pair]

        return {'id': _id, 'pair': pair, 'freq': freq, 'kwrd': kwrd}


if __name__ == "__main__":

    import logging.config
    import yaml

    with open('./trading_bot/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    cli = CLI('./strategies/')
    with cli:
        cli.run()
