#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-03-17 12:23:25
# @Last modified by: ArthurBernard
# @Last modified time: 2020-03-29 23:38:59

""" A (very) light Command Line Interface. """

# Built-in packages
import logging
from pickle import Unpickler
from threading import Thread
import time

# Third party packages
from blessed import Terminal
import fynance as fy
# import numpy as np
import pandas as pd

# Local packages
from trading_bot._client import _ClientCLI
from trading_bot.performance import PnL
from trading_bot.tools.io import load_config_params


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


class _ResultManager:  # (ResultManager):
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

                txt_table += self._set_stats_result(
                    df.loc[_index],
                    period,
                    value['period'],
                    col={'price': value['pair'], 'value': key}
                    # sn=key,
                )

        txt_table += (['-'] * (1 + len(self.metrics)),)

        return '\nStatistics of results:\n' + _set_text(*txt_table)

    def _set_stats_result(self, df, head, period, col=None):
        """ Set statistics in a table with header. """
        # table = [['-'] * (1 + len(self.metrics)), [head]]
        table = []
        if col is None:
            col = {'price': 'underlying', 'value': 'strategy'}

        for k, a in col.items():
            table += [[str(a)] + self.set_statistics(df.loc[:, k].values, period)]

        return table

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
    txt_running_clients = ''

    def __init__(self, path, address=('', 50000), authkey=b'tradingbot'):
        # TODO : if trading bot not yet running => launch it
        super(CLI, self).__init__(address=address, authkey=authkey)
        self.logger = logging.getLogger(__name__)
        self.path = path
        self.term = Terminal()

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

        k = input(self.txt).lower().split(' ')
        # k = k.lower() if len(k) > 0 else ' '
        self.logger.debug('command: {}'.format(k))
        if k[0] == 'q':

            raise StopIteration

        elif k[0] == 'stop':
            if len(k) < 2 or k[1] in ['all', 'trading_bot']:

                return ['_stop', 'tradingbot']

            elif k[1] in running_strat:

                return ['_stop', k[1]]

        elif k[0] == 'start':
            if len(k) < 2:

                self.logger.error("With 'start' command you must specify a "
                                  "name of strategy_bot")

            else:

                return k[:2]

        elif k[0] == 'perf':
            if len(k) < 2:
                k += ['all']

                return k

            elif k[1] in running_strat:

                return k[:2]

        elif not k:

            return 'sb_update'

        else:
            self.logger.error("Unknown commands {}".format(k))

    def display(self):
        print(self.term.home + self.term.clear)
        self.logger.debug('display')
        print(self.txt_running_clients)
        if self.strat_bot:
            rm = _ResultManager(self.strat_bot)
            print(rm.get_current_stats())

        else:
            print('No strategy bot is running.')

        # for pair, list_name in self.pair.items():
        #    older_hist = {'name': None, 'available': 0}
        #    for name in list_pair:
        #        pnl = self.strat_bot[name]['pnl']
        #        rm = _ResultManager(pnl, **self.strat_bot[name]['kwrd'])

        #        availabe = pnl.index[-1] - pnl.index[0]
        #        if available > older_hist['availabe']:
        #            older_hist['availabe'] = available
        #            older_hist['name'] = name
        #        strat_value = pnl.value.values
        #        # todo : compute perf for strat

        #    pair_value = self.strat_bot[older_hist['name']]['pnl'].price.values
        #    # todo : compute perf for pair underlying

        # todo display performance table

    def listen_tbm(self):
        self.logger.debug('start listen TradingBotManager')
        for k, a in self.conn_tbm:
            if self.is_stop():
                self.conn_tbm.shutdown()

            elif k is None:

                continue

            self._handler_tbm(k, a)
            self.update()
            # TODO : display performances
            self.display()

        self.logger.debug('stop listen TradingBotManager')

    def run(self):
        # TODO : request running clients
        self._request_running_clients()
        for k in self:
            if k == 'sb_update':
                self.conn_tbm.send((k, None),)

            elif k[0] in ['perf', 'start', 'stop']:
                self.conn_tbm.send((k[0], k[1:]),)

            else:
                self.logger.error('Unknown command: {}'.format(k))

            time.sleep(1)

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
        # information received from TradingBotManager
        if k is None:
            pass

        elif k == 'sb_update':
            self.pair = {}
            self.strat_bot = {n: self._get_sb_dict(i, n) for i, n in a.items()}

        elif k == 'running_clients':
            self.txt_running_clients = ''
            for c, v in a.items():
                self.txt_running_clients += '{}: {}\n'.format(c, v)

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

    def _request_running_clients(self):
        self.conn_tbm.send(('get_running_clients', None),)


if __name__ == "__main__":

    import logging.config
    import yaml

    with open('./trading_bot/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    cli = CLI('./strategies/')
    with cli:
        cli.run()
