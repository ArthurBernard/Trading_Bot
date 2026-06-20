#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-25 10:38:17
# @Last modified by: ArthurBernard
# @Last modified time: 2020-08-13 22:20:17

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
    columns = ['price', 'returns', 'volume', 'exchanged_volume', 'position',
               'signal', 'delta_signal', 'fee', 'PnL', 'cumPnL', 'value']

    def __init__(self, data, v0=None):
        """ Initialize the perf object.

        Parameters
        ----------
        data : pd.DataFrame
            DataFrame containing the orders history.
        v0 : float, optional
            Initial value available of the trading strategy.

        """
        self.logger = logging.getLogger('performance.PnL')
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

            self.logger.error('position at t + 1 does not match signal at t')
            # raise ValueError('position at t + 1 does not match signal at t')

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

    def _repr_html_(self):
        """ Return a html representation for a particular DataFrame. """
        if self.df is not None:

            return self.df._repr_html_()

        return "None"


class _PnLR(_PnLI):
    """ Object to compute PnL of only one asset. """

    _handler = {
        'price': 'price_exec',
        'volume': 'vol_exec',
        'd_signal': 'type',
        'fee': 'fee',
    }
    columns = ['price', 'returns', 'volume', 'exchanged_volume', 'position',
               'signal', 'delta_signal', 'fee', 'PnL', 'cumPnL', 'value',
               'slippage']

    def __init__(self, data, v0=True):
        """ Initialize the perf object.

        Parameters
        ----------
        data : pd.DataFrame
            DataFrame containing the orders history.
        v0 : float, optional
            Initial value available of the trading strategy.

        """
        exch_vol = self._get_exch_vol(data)
        self.p_init = _PnLI._get_price(_PnLI, data, exch_vol)
        super(_PnLR, self).__init__(data, v0)

    def _set_df(self):
        super(_PnLR, self)._set_df()
        self.slippage = self._get_slippage(
            self.price, self.d_signal, self.exch_vol, self.p_init
        )
        self.df.loc[:, 'slippage'] = self.slippage

    def _get_fee(self, data, *args):
        df = data.loc[:, (self._handler['fee'], 'TS')]

        return df.groupby(by='TS').sum().values

    def _get_slippage(self, price, d_signal, exch_vol, p_init):

        return (p_init - price) * exch_vol * np.sign(d_signal)


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
        self.logger = logging.getLogger('performance.FullPnL')
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
        if real:
            self._fillna('slippage', value=0.)

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

            self.logger.error('position at t + 1 does not match signal at t')
            # raise ValueError('position at t + 1 does not match signal at t')

    def __setitem__(self, key, value):
        self.df.loc[:, key] = value

    def __getitem__(self, key):
        return self.df.loc[:, key]

    def __repr__(self):
        """ Represent method. """
        return self.df.__repr__()

    def _repr_html_(self):
        """ Return a html representation for a particular DataFrame. """
        if self.df is not None:

            return self.df._repr_html_()

        return "None"


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
    save

    TODO
    ----
    If necessary, methods to save and update.

    """

    def __init__(self, path, timestep=None, v0=None, real=True,
                 name='orders_hist'):
        """ Initialize a FullPnl object.

        Parameters
        ----------
        path : str
            Path to load orders data.
        v0 : float
            Initial value available of the trading strategy.
        timestep : int
            Minimal number of seconds between two observations.
        real : bool, optional
            Set to False if the trading bot is in valide mode.
        name : str
            Name of file to load.

        """
        if path[-1] != '/':
            path += '/'

        self.path = path
        self.name = name
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
        # FIXME
        v = self.df.value.iloc[-1]
        p = self.df.price.iloc[-1]

        return round(float(v / p), 8)

    def _load(self):
        # load orders
        orders = get_df(path=self.path, name=self.name, ext='.dat')
        if orders.empty:

            return orders, pd.DataFrame(columns=['TS', 'price'])

        orders = orders.drop(columns=['txid', 'path', 'strat_name'])

        path = self.path + 'price.txt'
        # load prices
        prices = pd.read_csv(path, sep=',', names=['TS', 'price'])

        return orders.sort_values('userref'), prices.set_index('TS')

    def save(self):
        """ Save PnL. """
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
    q_tpm

    Methods
    -------
    loop

    """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize TradingPerformanceManager.

        Parameters
        ----------
        address : tuple of str and int
            Address of server.
        authkey : str
            Password.

        """
        super(TradingPerformanceManager, self).__init__(
            address=address,
            authkey=authkey
        )
        self.logger = logging.getLogger('performance')

    def __iter__(self):
        """ Iterate. """
        return self

    def __next__(self):
        """ Next method. """
        if self.is_stop():

            raise StopIteration

        elif not self.q_tpm.empty():

            return self.q_tpm.get()

        return None

    def loop(self):
        """ Loop until stop. """
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
