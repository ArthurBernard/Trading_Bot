#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-28 16:47:55
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-20 18:00:10

""" Clients to connect to TradingBotServer. """

# Built-in packages
import os
from random import randrange

# Third party packages

# Local packages
from trading_bot._connection import Connection, ConnDict
from trading_bot._server import TradingBotServer as TBS
from trading_bot.data_requests import DataBaseManager, DataExchangeManager


class _ClientBot:
    """ Base object for a client bot. """

    _handler = {
        'market': 'fees',
        'limit': 'fees_maker',
    }

    def __init__(self, address=('', 50000), authkey=b'tradingbot', _id=None):
        """ Initialize a client object and connect to the TradingBotServer. """
        self.id = randrange(1, 1e9) if _id is None else _id
        print('Id {} | Module {} | process ID is {} | parent PID is {}'.format(
            self.id, __name__, os.getpid(), os.getppid()
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
        # setup connection to TBM
        self.conn_tbm.setup(
            self.m.get_reader_tbm(self.id),
            self.m.get_writer_tbm(self.id)
        )
        # get queue to send conn to OrdersManager
        self.q_ord = self.m.get_queue_orders()
        # get state of server process
        self.p_state = self.m.get_state()
        # get queue to notify new client to TBM
        self.q_to_tbm = self.m.get_queue_cli_to_tbm()
        # notify new client to TBM
        self.q_to_tbm.put((self.id, 'up'),)

    def __exit__(self, exc_type, exc_value, exc_tb):
        # shutdown connection to TBM
        self.conn_tbm.shutdown(msg=exc_type)

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
        self.conn_tbm = Connection(self.id, name='strat')

    def __enter__(self):
        super(_ClientStrategyBot, self).__enter__()
        self.p_fees = self.m.get_proxy_fees()

    def get_fee(self, pair, order_type):
        """ Get current the fee for a pair and an conn type.

        Parameters
        ----------
        pair : str
            Symbol of the currency pair.
        order_type : str
            Type of conn.

        Returns
        -------
        float
            Fee of specified pair and conn type.

        """
        fees = self.p_fees._getvalue()
        if fees:

            return float(fees[self._handler[order_type]][pair]['fee'])

        else:

            return 0.0


class _ClientOrdersManager(_ClientBot):
    """ Base class for an conn manager. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a client object and connect to TradingBotServer. """
        _ClientBot.__init__(self, address=address, authkey=authkey, _id=0)
        self.conn_tbm = Connection(self.id, name='order_manager')
