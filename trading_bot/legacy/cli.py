#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-03-17 12:23:25
# @Last modified by: ArthurBernard
# @Last modified time: 2020-08-21 11:07:53

""" A (very) light Command Line Interface. """

# Built-in packages
import logging
import select
import sys
from threading import Thread
import time

# Third party packages
from blessed import Terminal
import fynance as fy
import numpy as np
import pandas as pd

# Local packages
from trading_bot._client import _ClientCLI
from trading_bot.data_requests import get_close
from trading_bot.tools.io import load_config_params, get_df


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
                k_list[j] += str(arg[i]) + space + ' |'
                if i < n - 1:
                    k_list[j] += ' '

            elif len(arg[0]) == 1 and len(arg) >= i + 1:
                k_list[j] += arg[i] * (n_spa + 2) + '+'

            else:
                if i % 2 == 0:
                    k_list[j] = k_list[j][:-2] + ' ' * (n_spa + 3) + '|'

                else:
                    k_list[j] = '|' + ' ' * (n_spa + 3) + k_list[j][1:-1]

                if i < n - 1:
                    k_list[j] += ' '

            j += 1

    return '\n'.join(k_list)


def _zip_text(txt1, txt2, c='  '):
    txt1 = txt1.split('\n')
    txt2 = txt2.split('\n')
    if len(txt1) < len(txt1):
        txt1, txt2 = txt2, txt1

    n = len(txt2)
    txt = list(a + c + b for a, b in zip(txt1[:n], txt2))
    txt += txt1[n:]

    return '\n'.join(txt)


def _rounder(*args, dec=0):
    """ Round each element of a list. """
    # return [round(float(arg), dec) for arg in args]
    return [round(float(a), dec) if abs(float(a)) < 10e3 else format(a, "5.1e") for a in args]


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

    min_freq = None
    min_TS = None
    max_TS = None

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
        self.strat_by_pair = {}
        for key, value in pnl_dict.items():
            idx = value['pnl'].index
            if self.max_TS is None or idx[-1] > self.max_TS:
                self.max_TS = idx[-1]

            self._set_ref_pair(key, value['pair'], value['freq'], idx.min())
            ts = (idx[1:] - idx[:-1]).min()
            self.pnl[key]['period'] = period * 86400 / ts

        index = range(self.min_TS, self.max_TS + 1, self.min_freq)
        columns = ['value', 'slippage', 'fee']
        self.tot_val = pd.DataFrame(0, index=index, columns=columns)
        for k, v in pnl_dict.items():
            df = pd.DataFrame(index=index, columns=columns)
            idx = v['pnl'].index
            if v['vali']:
                v['pnl'].loc[:, 'slippage'] = 0.

            df.loc[idx, 'value'] = v['pnl'].value.values
            df.loc[idx, 'fee'] = v['pnl'].fee.values
            df.loc[idx, 'slippage'] = v['pnl'].slippage.values
            df.loc[:, 'slippage'] = df.loc[:, 'slippage'].fillna(value=0.)
            df.loc[:, 'fee'] = df.loc[:, 'fee'].fillna(value=0.)
            df = df.fillna(method='ffill').fillna(method='bfill')
            self.tot_val.loc[:, 'value'] += df.value.values
            self.tot_val.loc[:, 'slippage'] += df.slippage.values
            self.tot_val.loc[:, 'fee'] += df.fee.values

        self.metrics = ['return', 'perf', 'sharpe', 'calmar', 'maxdd']
        self.periods = ['daily', 'weekly', 'monthly', 'yearly', 'total']
        self.logger = logging.getLogger(__name__)

    def get_current_stats(self):
        """ Display some statistics for some time periods. """
        txt_table = [['-'] * (1 + len(self.metrics) + 2),
                     ['   '] + self.metrics + ['slippage', 'cumFees']]
        self._update_pnl()

        for period in self.periods:
            txt_table += [['-'] * (1 + len(self.metrics) + 2), [period]]
            # for key, value in self.pnl.items():
            for pair, strats_dict in self.strat_by_pair.items():
                strat_ref = self.pnl[strats_dict['ref']]
                df = strat_ref['pnl']
                txt_table += self.set_stats_result(
                    df, period, strat_ref['period'], col={'price': '- ' + pair}
                )
                for key in strats_dict['strat']:
                    value = self.pnl[key]
                    df = value['pnl']
                    txt_table += self.set_stats_result(
                        df,
                        period,
                        value['period'],
                        col={'value': key}
                    )

            txt_table += self.set_stats_result(
                self.tot_val, period, 365, col={'value': 'total'}
            )

        txt_table += (['-'] * (1 + len(self.metrics) + 2),)

        return txt_table

    def set_stats_result(self, df, head, period, col):
        _index = self._get_period_index(df, head)
        if _index is None:

            return ''

        return self._set_stats_result(df.loc[_index], head, period, col=col)

    def _set_stats_result(self, df, head, period, col=None):
        """ Set statistics in a table with header. """
        # table = [['-'] * (1 + len(self.metrics)), [head]]
        table = []
        if col is None:
            col = {'price': 'underlying', 'value': 'strategy'}

        for k, a in col.items():
            table += [[str(a)] + self.set_statistics(df.loc[:, k].values,
                                                     period)]
            # Append slippage and fees
            if k == 'price':
                table[-1] += [' ', ' ']

            elif k == 'value':
                slippage = np.sum(df.loc[:, 'slippage'].values)
                cum_fees = np.sum(df.loc[:, 'fee'].values)
                table[-1] += _rounder(slippage, cum_fees, dec=2)

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

        # NOT CLEAN SOLUTION
        # if _index.sum() < 2:
        #    _index = df.index >= df.index[-2]

        return _index

    def _set_ref_pair(self, _id, pair, freq, TS_0):
        if self.min_freq is None or freq < self.min_freq:
            self.min_freq = freq

        if self.min_TS is None or TS_0 < self.min_TS:
            self.min_TS = TS_0

        if pair not in self.strat_by_pair:
            self.strat_by_pair[pair] = {'strat': []}

        f = self.strat_by_pair[pair].get('freq')
        t = self.strat_by_pair[pair].get('TS_0')

        if f is None or freq < f or (freq == f and TS_0 < t):
            self.strat_by_pair[pair]['freq'] = freq
            self.strat_by_pair[pair]['TS_0'] = TS_0
            self.strat_by_pair[pair]['ref'] = _id

        self.strat_by_pair[pair]['strat'] += [_id]

    def _update_pnl(self):
        pairs = ','.join(list(self.strat_by_pair.keys()))
        self.close = get_close(pairs)
        if not isinstance(self.close, dict):
            self.close = {pairs: self.close}

        total_ret = 0.
        t = int(time.time() / self.min_freq + 1) * self.min_freq
        for pair, strats_dict in self.strat_by_pair.items():
            close = self.close[pair]
            for strat in strats_dict['strat']:
                df = self.pnl[strat]['pnl']
                T = df.index[-1]
                if T == t:
                    df = df.drop(T, axis=0)

                df = update_pnl(df, close, t)
                self.pnl[strat]['pnl'] = df
                total_ret += df.loc[t, 'PnL']

        val = self.tot_val.value.iloc[-1]
        self.tot_val.loc[t, 'value'] = val + total_ret
        self.tot_val.loc[t, ('slippage', 'fee')] = 0, 0


def update_pnl(df, close, t):
    """ Update PnL dataframe with closed price. """
    T = df.index[-1]
    ret = close - df.loc[T, 'price']
    vol = df.loc[T, 'volume']
    pos = df.loc[T, 'signal']
    df.loc[t, 'price'] = close
    df.loc[t, 'returns'] = ret
    df.loc[t, 'volume'] = vol
    df.loc[t, 'position'] = pos
    df.loc[t, 'exchanged_volume'] = 0
    df.loc[t, 'signal'] = pos
    df.loc[t, 'delta_signal'] = 0
    df.loc[t, 'fee'] = 0
    df.loc[t, 'PnL'] = ret * vol * pos
    df.loc[t, 'cumPnL'] = df.loc[T, 'cumPnL'] + df.loc[t, 'PnL']
    df.loc[t, 'value'] = df.loc[T, 'value'] + df.loc[t, 'PnL']
    df.loc[t, 'slippage'] = 0

    return df


class CLI(_ClientCLI):
    """ Object to allow a Command Line Interface. """

    # TODO : append 'perf' command on specified strategy
    #        append 'balance' command on specified pair currency
    txt = ('The following commands are supported, press <ENTER> at the end.\n'
           '  - <q> quit the command line interface.\n'
           '  - <start [strategy_name]> run the specified strategy bot.\n'
           '  - <stop [strategy_name]> interupt the specified strategy bot.\n'
           '  - <stop> interupt the TradingBotManager.\n'
           '  - <ENTER> update the KPI of current running strategy bot.\n'
           "If no commands are received after 30 seconds, the CLI exited.")
    TIMEOUT = 30
    strat_bot = {}
    pair = {}
    txt_running_clients = ''
    running_strats = {}

    def __init__(self, path, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a CLI object. """
        # TODO : if trading bot not yet running => launch it
        super(CLI, self).__init__(address=address, authkey=authkey)
        self.logger = logging.getLogger('cli')

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

        time.sleep(0.15)
        print(self.txt)
        i, o, e = select.select([sys.stdin], [], [], self.TIMEOUT)
        if i:
            k = sys.stdin.readline().strip('\n').lower().split(' ')
            self.logger.debug('command: {}'.format(k))
            self._request_running_clients()
            time.sleep(0.1)

        else:
            self.logger.debug('Time out, CLI exit')
            k = ['q']

        if k[0] == 'q':

            raise StopIteration

        elif k[0] == 'stop':
            if len(k) < 2 or k[1] in ['all', 'trading_bot']:

                return ['_stop', 'tradingbot']

            elif k[1] in self.running_strats:

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

            elif k[1] in self.running_strats:

                return k[:2]

        elif not k[0]:

            return 'sb_update'

        self.logger.error("Unknown commands {}".format(k))

    def display(self):
        print(self.term.home + self.term.clear)
        self.logger.debug('display')
        strat_val = [['-'] * 3, ['Strategies', 'Values', 'Volumes'], ['-'] * 3]
        for s, args in self.strat_values.items():
            strat_val += [[s, '{:.2f}'.format(args['value']),
                           '{:.8f}'.format(args['volume'])]]

        strat_val += [['-'] * 3]
        if self.strat_values:
            txt_strat = _set_text(*strat_val)

        txt_clients = self.txt_running_clients
        strat = {
            k: v for k, v in self.strat_bot.items() if v.get('pnl') is not None
        }
        if strat:
            rm = _ResultManager(strat)
            txt_stats = _set_text(*rm.get_current_stats())
            close = rm.close
            txt_close = [['-'] * 2, ['Pair', 'Close'], ['-'] * 2]
            for pair, price, in close.items():
                txt_close += [[pair, price], ['-'] * 2]

            txt_close = txt_close[:-1] + [['-'] * 2]
            txt_close = _set_text(*txt_close)
            txt_balance = _set_text(*self._set_text_balance())
            txt_pos = _set_text(*self._set_text_position())
            txt = _zip_text(
                txt_stats,
                txt_close + '\n\n' + txt_strat + '\n\n' + txt_clients + '\n\n' + txt_balance
            )
            print(txt)
            print(txt_pos)

        else:
            print(txt_clients + 'No strategy bot is running.')

    def listen_tbm(self):
        self.logger.debug('start listen TradingBotManager')
        for k, a in self.conn_tbm:
            if self.is_stop():
                self.conn_tbm.shutdown()

            elif k is None:

                continue

            self._handler_tbm(k, a)
            self.update()
            if k == 'sb_update':
                # TODO : display performances
                self.display()

        self.logger.debug('stop listen TradingBotManager')

    def run(self):
        # TODO : request running clients
        self._request_running_clients()
        for k in self:
            if k is None:

                continue

            elif k == 'sb_update':
                self.conn_tbm.send((k, None),)

            elif k[0] in ['perf', 'start', '_stop']:
                self.conn_tbm.send((k[0], k[1:]),)

            else:
                self.logger.error('Unknown command: {}'.format(k))

            time.sleep(0.1)

    def update(self):
        self.logger.debug('update start')
        self.strat_values = {}
        for k in self.strat_bot:
            txt = 'update {}'.format(k)
            pnl = get_df(self.path + k, 'PnL', ext='.dat')
            if pnl.empty:

                continue

            value = pnl.value.iloc[-1]
            self.strat_values[k] = {'value': value,
                                    'volume': value / pnl.price.iloc[-1]}
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
            self.running_strats = a['strategy_bots']
            self.txt_running_clients = ''
            for c, v in a.items():
                if c == 'strategy_bots':
                    if not v:

                        continue

                    self.txt_running_clients += c + ':\n'
                    for sc, sv in v.items():
                        self.txt_running_clients += '{} is {}\n'.format(sc, sv)

                else:
                    self.txt_running_clients += '{} is {}\n'.format(c, v)

        elif k == 'balance':
            self.logger.info('Receive balance: {}'.format(a))
            self.balance = a

        elif k in ['cpos', 'cvol']:
            for key, args in self.strat_bot.items():
                if args['id'] == a[0]:
                    self.strat_bot[key][k] = a[1]

        else:
            self.logger.error('received unknown message {}: {}'.format(k, a))

    def _get_sb_dict(self, _id, name):
        # load some configuration info
        sb_dict = {'id': _id}
        cfg = load_config_params(self.path + name + '/configuration.yaml')
        sb_dict['pair'] = pair = cfg['order_instance']['pair']
        sb_dict['vali'] = cfg['order_instance'].get('validate', False)
        sb_dict['freq'] = cfg['strat_manager_instance']['frequency']
        sb_dict['kwrd'] = cfg['result_instance']
        self.conn_tbm.send(('get_pos', _id),)
        self.conn_tbm.send(('get_vol', _id),)
        sb_dict['cpos'] = cfg['strat_manager_instance']['current_pos']
        sb_dict['cvol'] = cfg['strat_manager_instance']['current_vol']
        if pair not in self.pair:
            self.pair[pair] = []

        self.pair[pair] += [pair]

        return sb_dict

    def _request_running_clients(self):
        self.conn_tbm.send(('get_running_clients', None),)
        time.sleep(0.1)

    def _set_text_balance(self):
        ccy = []
        for pair in self.pair:
            c1, c2 = pair[:4], pair[4:]
            ccy = ccy + [c1] if c1 not in ccy and c1 in self.balance else ccy
            ccy = ccy + [c2] if c2 not in ccy and c2 in self.balance else ccy

        txt_list = [('-', '-'), ('Currency', 'Balance'), ('-', '-')]
        for c in ccy:
            txt_list += [[c] + _rounder(self.balance[c], dec=8)]

        return txt_list + [('-', '-')]

    def _set_text_position(self):
        txt_list = [['-'] * 6,
                    ['Strategy', 'Real Position', 'Theorical Position', 'Rvol', 'Rvol2', 'Thvol'],
                    ['-'] * 6]
        for name, kwargs in self.strat_bot.items():
            pnl = kwargs['pnl']
            re_pos = pnl.position.iloc[0] + pnl.delta_signal.sum()
            th_pos = kwargs['cpos']
            re_vol = pnl.volume.iloc[0] + (pnl.PnL / pnl.price).sum()
            re_vol_2 = pnl.volume.iloc[0] + (pnl.exchanged_volume * np.sign(pnl.delta_signal)).sum()
            th_vol = kwargs['cvol']
            txt_list += [[name, re_pos, th_pos, re_vol, re_vol_2, th_vol]]

        return txt_list + [['-'] * 6]


if __name__ == "__main__":

    import logging.config

    # Load logging configuration
    log_config = load_config_params('./trading_bot/logging.ini')
    logging.config.dictConfig(log_config)

    # Load general configuration
    gen_config = load_config_params('./general_config.yaml')
    path = gen_config['path']['strategy']

    try:
        cli = CLI(path)

    except ConnectionRefusedError:
        txt = 'TradingBotManager is not running, do you want to run it? Y/N'
        while True:
            a = input(txt)
            if a[0].lower() == 'y':
                # TODO : run TradingBotServer
                print('not yet implemented')
                break

            elif a[0].lower() == 'n':
                exit()

            else:
                print('Unknown command: {}. Answere with yes or no.'.format(a))

    with cli:
        cli.run()
