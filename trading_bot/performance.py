#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-25 10:38:17
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-26 18:57:20

""" Objects to measure and display trading performance. """

# Built-in packages
import logging

# Third party packages

# Local packages
from trading_bot._client import _ClientTradingPerformance
from trading_bot.tools.io import get_df


class _Performance:
    """ Object to compute performance of a strategy. """

    def __init__(self, path='.', name='orders_hist', ext='.dat', t=0):
        self.df = get_df(path, name, ext)  # .reset_index()
        self.t0 = t
        i = self.df.index[t]
        if self.df.loc[i, 'ex_vol'] != 0:
            self.val_0 = self.df.loc[i, 'ex_vol'] * self.df.loc[i, 'price']

        else:
            raise ValueError('ex_vol = 0')

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
        if 'fee_pct' in self.df.columns:
            self.fee = self.df.loc[self.i, 'fee_pct']

        else:
            self.fee = None

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
        f = self.df.loc[self.i, 'fee_pct'] if self.fee is not None else 0.
        self.pos += s
        self.vol_quote -= (s + f / 100) * v * p
        self.vol_base += s * v
        self.val = self.vol_base * p + self.vol_quote
        self.pnl_1 = self.val - self.val_1
        self.pnl += self.pnl


class TradingPerformance(_ClientTradingPerformance):
    """ TradingPerformance object. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        super(TradingPerformance, self).__inti__(
            address=address,
            authkey=authkey
        )
        self.logger = logging.getLogger(__name__)
