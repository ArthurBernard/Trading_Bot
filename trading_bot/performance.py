#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-25 10:38:17
# @Last modified by: ArthurBernard
# @Last modified time: 2020-03-18 08:51:38

""" Objects to measure and display trading performance. """

# Built-in packages
import logging
from pickle import Pickler
import time

# Third party packages
import fynance as fy
import numpy as np
import pandas as pd

# Local packages
from trading_bot._client import _ClientPerformanceManager
from trading_bot.tools.io import get_df


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
        self.columns = ['price', 'returns', 'volume', 'exchanged_volume',
                        'position', 'signal', 'delta_signal', 'fee', 'PnL',
                        'cumPnL', 'value']
        self.index = data.loc[:, 'TS'].drop_duplicates()
        if v0 is None and data.ex_vol[0] != 0.:
            self.v0 = data.ex_vol[0] * data.price[0]

        elif v0 is None:
            self.v0 = data.volume[0] * data.price[0]

        else:
            self.v0 = v0

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
        self.value = self.cumpnl + self.v0
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
        self.df.loc[:, 'volume'] = self.vol_pos
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
        pv = df.groupby(by='TS').sum().values

        return pv / exch_vol

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
    def __init__(self, orders, prices=None, timestep=None, v0=None, real=True):
        """ Initialize a FullPnl object.

        Parameters
        ----------
        orders : pd.DataFrame
            DataFrame containing the orders history.
        prices : pd.DataFrame or pd.Series
            Series of prices.

        """
        orders = orders.sort_values('userref').reset_index(drop=True)
        t_idx = orders.loc[:, 'TS'].drop_duplicates()
        self.t0, T = t_idx.min(), t_idx.max()
        if timestep is None:
            self.ts = int(t_idx.sort_values().diff().min())

        else:
            self.ts = timestep

        if prices is not None:
            self.T = max(prices.index.max(), T)

        else:
            self.T = T

        self.index = range(self.t0, self.T + 1, self.ts)
        if real:
            pnl = _PnLR(orders, v0=v0)

        else:
            pnl = _PnLI(orders, v0=v0)

        self.df = pd.DataFrame(index=self.index, columns=pnl.columns)
        self.df.loc[pnl.index, :] = pnl.df.values
        self._fillna('volume', 'signal', method='ffill')
        self._fillna('exchanged_volume', 'delta_signal', 'fee', value=0.)
        self._fillna('position', method='bfill')
        self._fillna('position', value=self['signal'].values[-1])
        self._check_signal_position(T=T)
        self._fillna_price(prices)
        self['returns'] = self['price'].diff().fillna(value=0).values
        self._set_pnl()
        self['cumPnL'] = np.cumsum(self['PnL'].values)
        self['value'] = self['cumPnL'].values + pnl.v0

    def _set_pnl(self):
        pnl = self[('volume', 'returns', 'position')].prod(axis=1).values
        self['PnL'] = pnl - self['fee']

    def _fillna(self, *args, **kwargs):
        self.df.loc[:, args] = self.df.loc[:, args].fillna(**kwargs)

    def _fillna_price(self, prices):
        if prices is not None:
            prices = prices.loc[self.t0:]
            na_idx = prices.index[self.df.loc[prices.index, 'price'].isna()]
            self.df.loc[na_idx, 'price'] = prices.loc[na_idx, 'price'].values

        self._fillna('price', method='ffill')

    def _check_signal_position(self, T):
        if not np.array_equiv(
            self.df.loc[self.t0 + self.ts: T, 'position'].values,
            self.df.loc[self.t0: T - self.ts, 'signal'].values
        ):

            raise ValueError('position at t + 1 does not match signal at t')

    def __setitem__(self, key, value):
        self.df.loc[:, key] = value

    def __getitem__(self, key):
        return self.df.loc[:, key]


class PnL(_FullPnL):
    """ Object to compute profit and loss of trading bot.

    Attributes
    ----------
    df : pandas.DataFrame
        Data with each series to compute profit and loss.
    ts : int
        Number of seconds between two observations.
    t0, T : int
        Respectively first and last trade.

    Methods
    -------
    get_current_volume
    load

    TODO
    ----
    If necessary, methods to save and update.

    """

    def __init__(self, path, timestep=None, v0=None, real=True):
        """ Initialize a FullPnl object.

        Parameters
        ----------
        v0 : float
            Initial value available of the trading strategy.
        timestep : int
            Minimal number of seconds between two observations.
        real : bool, optional
            Set to False if the trading bot is in valide mode.

        """
        if path[-1] != '/':
            path += '/'

        self.path = path
        # try:
        #    # load pnl
        #    with open(self.path, 'rb') as f:
        #        self.df = Unpickler(f).load()

        #    self.index = self.df.index
        #    self.t0, self.T = self.index.min(), self.index.max()
        #    if timestep is None:
        #        self.ts = int(t_idx.sort_values().diff().min())

        #    else:
        #        self.ts = timestep

        # except FileNotFoundError:
        orders, prices = self._load()
        if orders.index.size > 2:
            super(PnL, self).__init__(
                orders, prices, v0=v0, timestep=timestep, real=real
            )

        else:
            self.df = None

    def get_current_volume(self):
        """ Get current volume of the portfolio strategy.

        Returns
        -------
        float
            Current volume of the portfolio.

        """
        v = self.df.value.iloc[-1]
        p = self.df.price.iloc[-1]

        return round(float(v / p), 8)

    def _load(self):
        # load orders
        orders = get_df(path=self.path, name='orders_hist', ext='.dat')
        orders = orders.drop(columns=['txid', 'path', 'strat_name'])

        path = self.path + 'price.txt'
        # load prices
        prices = pd.read_csv(path, sep=',', names=['TS', 'price'])

        return orders.sort_values('userref'), prices.set_index('TS')

    def save(self):
        if self.df is not None:
            with open(self.path + 'PnL.dat', 'wb') as f:
                Pickler(f).dump(self.df)

        else:
            print('not yet dataframe PnL to save')


# TODO:
# Set client perf manager
#   receive list of strategy to manage
#   update value available (pnl)
# Set displayer manager
#   print performances
#   CLI


class TradingPerformanceManager(_ClientPerformanceManager):
    """ TradingPerformanceManager object compute performances of trading bots.

    Attributes
    ----------

    Methods
    -------

    """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        super(TradingPerformanceManager, self).__init__(
            address=address,
            authkey=authkey
        )
        self.logger = logging.getLogger(__name__)

    def __iter__(self):
        return self

    def __next__(self):
        if self.is_stop():

            raise StopIteration

        elif not self.q_tpm.empty():

            return self.q_tpm.get()

        return None

    def loop(self):
        self.logger.info('Start loop TradingPerformanceManager')
        # while not self.is_stop():
        for kwrds in self:
            # if self.q_tpm.empty():
            if kwrds is None:
                time.sleep(0.01)

                continue

            # kwrds = self.q_tpm.get()
            path = kwrds['path']
            name = path.split('/')[-1]
            self.logger.info('receive info to compute PnL {}'.format(name))
            pnl = PnL(**kwrds)
            pnl.save()
            if pnl.df is not None:
                v = pnl.get_current_volume()
                if path[-1] != '/':
                    path += '/'

                with open(path + 'current_volume.dat', 'wb') as f:
                    Pickler(f).dump(v)

                self.logger.info('Current volume updated: {}'.format(name))

        self.logger.info('Stop loop TradingPerformanceManager')

    def _add_pnl(self, _id):
        # add a new pnl to compute
        pass

    def _rm_pnl(self, _id):
        # remove a pnl to compute
        pass


# DEPRECATED OBJECT AND FUNCTION #


class _dep_ResultManager:
    """ Manager object of historical results of strategy.

    Attributes
    ----------
    df : pandas.DataFrame
        Data with each series to compute performances.
    period : int
        Maximal number of trading periods per year
    metrics : list of str
        List of metrics to compute performance. The following are available:
        'return', 'perf', 'sharpe', 'calmar', and 'mdd'.
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

        # self.logger.info(txt)

        return txt

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


def _dep_set_text(*args):
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


def _dep_rounder(*args, dec=0):
    """ Round each element of a list. """
    return [round(float(arg), dec) for arg in args]


if __name__ == '__main__':
    # Load logging configuration
    import logging.config
    import yaml

    with open('./trading_bot/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    # Start running a trading performance manager
    tpm = TradingPerformanceManager()
    with tpm:
        tpm.loop()
