#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-28 16:47:55
# @Last modified by: ArthurBernard
# @Last modified time: 2020-01-28 19:47:14

# Built-in packages

# Third party packages

# Local packages
from _server import TradingBotServer as TBS


class _Client:
    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a client object and connect to the TradingBotServer. """
        TBS.register('get_queue_orders')
        TBS.register('get_proxy_fees')
        TBS.register('_get_pipe_om_tbm')
        self.m = TBS(address=address, authkey=authkey)
        self.m.connect()
        self.q_ord = self.m.get_queue_orders()
        self.p_fees = self.m.get_proxy_fees()

    def _get_fees(self, pair, order_type):
        """ Get current fees of order.

        Parameters
        ----------
        pair : str
            Symbol of the currency pair.
        order_type : str
            Type of order.

        Returns
        -------
        float
            Fees of specified pair and order type.

        """
        if order_type == 'market':

            return float(self.fees['fees'][pair]['fee'])

        elif order_type == 'limit':

            return float(self.fees['fees_maker'][pair]['fee'])

        else:

            raise ValueError('Unknown order type: {}'.format(order_type))
