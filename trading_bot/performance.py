#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-25 10:38:17
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-28 18:54:19

""" Objects to measure and display trading performance. """

# Built-in packages
import logging

# Third party packages
import fynance as fy
import numpy as np
import pandas as pd

# Local packages
from trading_bot._client import _ClientTradingPerformance
# from trading_bot.tools.io import get_df


class _PnLI:
    """ Object to compute performance of only one asset. """

    _handler = {
        'price': 'price',
        'volume': 'volume',
        'd_signal': 'type',
        'fee': 'fee_pct',
    }

    def __init__(self, data, v0=None):
        """ Initialize the perf object.

        Parameters
        ----------
        data : pd.DataFrame
            DataFrame containing the orders history.
        v0 : float, optional
            Initial value available of the trading strategy.

        """
        self.columns = ['price', 'returns', 'volume_pos', 'exchanged_volume',
                        'position', 'signal', 'delta_signal', 'fee', 'PnL',
                        'cumPnL', 'value']
        self.index = data.loc[:, 'TS'].drop_duplicates()
        if v0 is None:
            v0 = data.ex_vol[0] * data.price[0] if data.ex_vol != 0. else 0.

        self.exch_vol = self._get_exch_vol(data)
        self.price = self._get_price(data, self.exch_vol)
        self.returns = self._get_returns()
        self.d_signal = self._get_delta_signal(data)
        self.fee = self._get_fee(data, self.price)  # self.exch_vol)
        self.signal = self._get_signal(self.d_signal, data.ex_pos[0])
        self.pos = self._get_pos(data)
        self.vol_pos = self._get_vol_pos(data)
        self.pnl = self._get_PnL(self.returns, self.pos, self.vol_pos,
                                 self.fee)
        self.cumpnl = np.cumsum(self.pnl)
        self.value = self.cumpnl + v0
        self._set_df()
        if (self.pos[1:] != self.signal[:-1]).any():

            raise ValueError('position at t + 1 does not match signal at t')

    def _set_df(self):
        self.df = pd.DataFrame(
            0,
            index=self.index,
            columns=self.columns
        )
        self.df.loc[:, 'exchanged_volume'] = self.exch_vol
        self.df.loc[:, 'price'] = self.price
        self.df.loc[:, 'returns'] = self.returns
        self.df.loc[:, 'delta_signal'] = self.d_signal
        self.df.loc[:, 'fee'] = self.fee
        self.df.loc[:, 'signal'] = self.signal
        self.df.loc[:, 'position'] = self.pos
        self.df.loc[:, 'volume_pos'] = self.vol_pos
        self.df.loc[:, 'PnL'] = self.pnl
        self.df.loc[:, 'cumPnL'] = self.cumpnl
        self.df.loc[:, 'value'] = self.value

    def __repr__(self):
        return self.df.__repr__()

    def _get_pos(self, data):
        df = data.loc[:, ('ex_pos', 'TS', 'userref')].sort_values(by='userref')
        df = df.drop_duplicates(subset='TS', keep='first')

        return df.loc[:, ('ex_pos',)].values

    def _get_vol_pos(self, data):
        df = data.loc[:, ('ex_vol', 'TS', 'userref')].sort_values(by='userref')
        df = df.drop_duplicates(subset='TS', keep='first')

        return df.loc[:, ('ex_vol',)].values

    def _get_exch_vol(self, data):
        df = data.loc[:, (self._handler['volume'], 'TS')]

        return df.groupby(by='TS').sum().values

    def _get_price(self, data, exch_vol):
        df = data.loc[:, (self._handler['price'], 'TS')]
        volume = data.loc[:, self._handler['volume']].values
        df.loc[:, self._handler['price']] *= volume

        return df.groupby(by='TS').sum().values / exch_vol

    def _get_returns(self):
        r = np.zeros(self.price.shape)
        r[1:] = self.price[1:] - self.price[:-1]

        return r

    def _get_delta_signal(self, data):
        df = data.loc[:, (self._handler['d_signal'], 'TS')]
        df.loc[:, 'd_signal'] = df.loc[:, self._handler['d_signal']].apply(
            lambda x: 1 if x == 'buy' else -1
        )

        return df.loc[:, ('d_signal', 'TS')].groupby('TS').sum().values

    def _get_signal(self, d_signal, pos_init):
        return np.cumsum(d_signal, axis=0) + pos_init

    def _get_fee(self, data, price):
        df = data.loc[:, (self._handler['fee'], 'TS')]
        volume = data.loc[:, self._handler['volume']].values
        df.loc[:, self._handler['fee']] *= volume

        return df.groupby(by='TS').sum().values * price / 100

    def _get_PnL(self, returns, pos, vol_pos, fee):
        return vol_pos * returns * pos - fee


class _PnLR(_PnLI):
    """ Object to compute PnL of only one asset. """

    _handler = {
        'price': 'price_exec',
        'volume': 'vol_exec',
        'd_signal': 'type',
        'fee': 'fee',
    }

    def _get_fee(self, data, *args):
        df = data.loc[:, (self._handler['fee'], 'TS')]

        return df.groupby(by='TS').sum().values


class _FullPnL:
    def __init__(self, data, p=None, v0=None, timestep=None, real=True):
        """ Initialize a FullPnl object.

        Parameters
        ----------
        data : pd.DataFrame
            DataFrame containing the orders history.
        v0 : float, optional
            Initial value available of the trading strategy.
        timestep : int, optional
            Minimal number of seconds between two observations.

        """
        self.t_idx = data.loc[:, 'TS'].drop_duplicates()
        self.t0, self.T = self.t_idx.min(), self.t_idx.max()
        if timestep is None:
            timestep = self.t_idx.sort_values().diff().min()

        # time_range = pd.date_range()
        self.index = range(self.t0, self.T + 1, timestep)
        if real:
            pnl = _PnLR(data, v0=v0)

        else:
            pnl = _PnLI(data, v0=v0)

        self.df = pd.DataFrame(index=self.index, columns=pnl.columns)
        self.df.loc[pnl.index, :] = pnl.df.values
        self._fillna('volume_pos', 'signal', method='ffill')
        self._fillna('exchanged_volume', 'delta_signal', 'fee', value=0.)
        self._fillna('position', method='bfill')
        self._check_signal_position()
        self._fillna_price(p)
        self['returns'] = self['price'].diff().fillna(value=0).values
        self._set_pnl()
        self['cumPnL'] = np.cumsum(self['PnL'].values)
        self['value'] = self['cumPnL'].values + v0

    def _set_pnl(self):
        pnl = self[('volume_pos', 'returns', 'position')].prod(axis=1).values
        self['PnL'] = pnl - self['fee']

    def _fillna(self, *args, **kwargs):
        self.df.loc[:, args] = self.df.loc[:, args].fillna(**kwargs)

    def _fillna_price(self, p):
        if p is not None:
            p.loc[self.t0: self.T]
            na_idx = p.index[self.df.loc[p.index, 'price'].isna()]
            self.df.loc[na_idx, 'price'] = p.loc[na_idx, 'price'].values

        self._fillna('price', method='ffill')

    def _check_signal_position(self):
        if (self['position'].values[1:] != self['signal'].values[:-1]).any():

            raise ValueError('position at t + 1 does not match signal at t')

    def __setitem__(self, key, value):
        self.df.loc[:, key] = value

    def __getitem__(self, key):
        return self.df.loc[:, key]


class ResultManager:
    """ Manager object of historical results of strategy.

    Methods
    -------
    update_result_hist(order_results)
        Load, merge and save result historic strategy.
    save_result_hist()
        Save historical results.
    print_stats()
        Print some statistics of historical results strategy.
    get_current_value()
        Get current value of the portfolio strategy.

    """

    def __init__(self, df, period=252, metrics=[], periods=[], t=0, fee=None):
        """ Initialize object.

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
            'monthly', 'yearly' and 'total'.
        reinvest_profit : bool, optional
            If true reinvest profit.

        """
        self.period = period
        self.metrics = metrics
        self.periods = periods
        self.perf = _TheoricPerformance(df)
        self.logger = logging.getLogger(__name__)

    def set_current_price(self):
        """ Display current price and fees. """
        txt = 'Display results\n' + _set_text(
            ['-'],
            ['Price of the underlying: {:.2f}'.format(self.df.price.iloc[-1])],
            ['Current fees: {:.2}%'.format(self.df.fee.iloc[-1])],
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

            txt_table += self._set_stats_result(self.df.loc[_index], period)

        txt_table += (['-'] * (1 + len(self.metrics)),)

        return '\nStatistics of results:\n' + _set_text(*txt_table)

    def print_stats(self):
        """ Print some statistics of historical results strategy. """
        txt = self.set_current_price()
        txt += self.set_current_value()
        txt += self.set_current_stats()

        self.logger.info(txt)

        return self

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

    def get_current_volume(self):
        """ Get current volume of the portfolio strategy.

        Returns
        -------
        float
            Current volume of the portfolio.

        """
        return float(self.df.value.iloc[-1] / self.df.price.iloc[-1])


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


class TradingPerformance(_ClientTradingPerformance):
    """ TradingPerformance object. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        super(TradingPerformance, self).__inti__(
            address=address,
            authkey=authkey
        )
        self.logger = logging.getLogger(__name__)


# =========================================================================== #
#                                 deprecated                                  #
# =========================================================================== #


class _TheoricPerformance:
    """ Object to compute theorical performance of a strategy. """

    def __init__(self, df, t=0, fee=None):
        self.df = df
        self.t0 = t
        i = self.df.index[t]
        if df.loc[i, 'ex_vol'] != 0:
            self.val_0 = df.loc[i, 'ex_vol'] * df.loc[i, 'price']

        else:
            raise ValueError('ex_vol = 0')

        if 'fee_pct' in self.df.columns:
            self.fee = 'update'

        elif fee is None:
            self.fee = 0.

        else:
            self.fee = fee

        self.pos_0 = self.df.loc[i, 'ex_pos']

    def __iter__(self):
        self.t = self.t0
        self.i = self.df.index[self.t]
        self.T = self.df.index.size
        self.pos = self.pos_0
        self.val = self.val_0
        self.vol_quote = (1 - self.pos) * self.val_0
        self.vol_base = self.pos * self.val_0 / self.df.loc[self.i, 'price']
        self.pnl = 0
        self._update()

        return self

    def __next__(self):
        self.t += 1
        if self.t >= self.T:

            raise StopIteration

        self.i = self.df.index[self.t]
        self._update()

    def __repr__(self):
        txt = ('Position={self.pos:2}, Base Volume={self.vol_base:8.6f}, Quote'
               ' Volume={self.vol_quote:8.2f}, Value={self.val:6.2f}, Pnl='
               '{self.pnl:6.2f}, DeltaPnL={self.pnl_1:6.2f}'.format(self=self))

        return txt

    def _update(self):
        self.val_1 = self.val
        s = 1 if self.df.loc[self.i, 'type'] == 'buy' else -1
        p = self.df.loc[self.i, 'price']
        v = self.df.loc[self.i, 'volume']
        f = self.df.loc[self.i, 'fee_pct'] if self.fee == 'update' else self.fee
        self.pos += s
        self.vol_quote -= (s + f / 100) * v * p
        self.vol_base += s * v
        self.val = self.vol_base * p + self.vol_quote
        self.pnl_1 = self.val - self.val_1
        self.pnl += self.pnl_1