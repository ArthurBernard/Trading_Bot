#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-28 16:47:55
# @Last modified by: ArthurBernard
# @Last modified time: 2020-01-29 15:08:35

""" Clients to connect to TradingBotServer. """

# Built-in packages
import os

# Third party packages

# Local packages
from _server import TradingBotServer as TBS


class _Client:
    """ Base class for a client. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a client object and connect to the TradingBotServer. """
        print('Module {}: process ID is {} and parent PID is {}'.format(
            __name__, os.getpid(), os.getppid()
        ))
        TBS.register('get_queue_orders')
        TBS.register('get_state')
        self.m = TBS(address=address, authkey=authkey)
        self.m.connect()
        self.q_ord = self.m.get_queue_orders()
        self.p_state = self.m.get_state()

    def is_stop(self):
        return self.p_state._getvalue()['stop']


class _BotClient(_Client):
    """ Base class for a trading bot. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a client object and connect to TradingBotServer. """
        TBS.register('get_proxy_fees')
        _Client.__init__(self, address=address, authkey=authkey)
        self.p_fees = self.m.get_proxy_fees()

    def get_fee(self, pair, order_type):
        """ Get current the fee for a pair and an order type.

        Parameters
        ----------
        pair : str
            Symbol of the currency pair.
        order_type : str
            Type of order.

        Returns
        -------
        float
            Fee of specified pair and order type.

        """
        if order_type == 'market':

            return float(self.p_fees._getvalue()['fees'][pair]['fee'])

        elif order_type == 'limit':

            return float(self.p_fees._getvalue()['fees_maker'][pair]['fee'])

        else:

            raise ValueError('Unknown order type: {}'.format(order_type))


class _OrderManagerClient(_Client):
    """ Base class for an order manager. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a client object and connect to TradingBotServer. """
        TBS.register('get_reader_tbm')
        TBS.register('get_writer_tbm')
        _Client.__init__(self, address=address, authkey=authkey)
        self.r_tbm = self.m.get_reader_tbm()
        self.w_tbm = self.m.get_writer_tbm()
