#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-28 16:47:55
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-19 15:39:29

""" Clients to connect to TradingBotServer. """

# Built-in packages
import os
from random import randrange

# Third party packages

# Local packages
from trading_bot._server import TradingBotServer as TBS
from trading_bot.data_requests import DataBaseManager, DataExchangeManager


class _ClientBot:
    """ Base object for a client bot. """

    _handler = {
        'market': 'fees',
        'limit': 'fees_maker',
    }

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a client object and connect to the TradingBotServer. """
        self._id = randrange(1, 1e9)
        print('Id {} | Module {} | process ID is {} | parent PID is {}'.format(
            self._id, __name__, os.getpid(), os.getppid()
        ))
        # register methods
        TBS.register('get_queue_orders')
        TBS.register('get_queue_cli_to_tbm')
        TBS.register('get_state')
        TBS.register('get_reader_tbm')
        TBS.register('get_writer_tbm')
        # authentication and connection to server
        self.m = TBS(address=address, authkey=authkey)
        self.m.connect()

    def __enter__(self):
        # get queue to send orders to OrdersManager
        self.q_ord = self.m.get_queue_orders()
        # get queue to notify new client to TBM
        self.q_to_tbm = self.m.get_queue_cli_to_tbm()
        # get state of server process
        self.p_state = self.m.get_state()
        # get reader and writer to TBM
        self.r_tbm = self.m.get_reader_tbm(self._id)
        self.w_tbm = self.m.get_writer_tbm(self._id)
        # notify new client to TBM
        self.q_to_tbm.put((self._id, 'up'),)

    def __exit__(self, exc_type, exc_value, exc_tb):
        # stop listen client into TBM
        self.w_tbm.send({'stop': exc_type})
        # close connections
        self.r_tbm.close()
        self.w_tbm.close()

    def is_stop(self):
        """ Check if the server is stopped. """
        return self.p_state._getvalue()['stop']


class _ClientStrategyBot(_ClientBot):
    """ Base class for a trading strategy bot. """

    _handler = {
        **_ClientBot._handler,
        'exchange': DataExchangeManager,
        'database': DataBaseManager,
    }

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a client object and connect to TradingBotServer. """
        TBS.register('get_proxy_fees')
        _ClientBot.__init__(self, address=address, authkey=authkey)

    def __enter__(self):
        super(_ClientStrategyBot, self).__enter__()
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
        fees = self.p_fees._getvalue()
        if fees:

            return float(fees[self._handler[order_type]][pair]['fee'])

        else:

            return 0.0


class _ClientOrdersManager(_ClientBot):
    """ Base class for an order manager. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a client object and connect to TradingBotServer. """
        # TBS.register('get_reader_tbm')
        # TBS.register('get_writer_tbm')
        _ClientBot.__init__(self, address=address, authkey=authkey)
        self._id = 0
        # self.r_tbm = self.m.get_reader_tbm()
        # self.w_tbm = self.m.get_writer_tbm()
