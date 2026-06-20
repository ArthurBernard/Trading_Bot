#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-28 16:47:55
# @Last modified by: ArthurBernard
# @Last modified time: 2020-05-09 16:13:59

""" Clients to connect to TradingBotServer. """

# Built-in packages
import logging
import os

# Third party packages

# Local packages
from trading_bot._connection import ConnTradingBotManager
# from trading_bot._containers import OrderDict
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
        self.logger = logging.getLogger(__name__)
        self.logger.info(
            'PID: {} | PPID: {}'.format(os.getpid(), os.getppid())
        )

        # register methods
        TBS.register('get_queue_orders')
        TBS.register('get_queue_sb_to_tpm')
        TBS.register('get_queue_cli_to_tbm')
        TBS.register('get_state')
        TBS.register('get_reader_tbm')
        TBS.register('get_writer_tbm')
        # authentication and ConnectionTradingBotManager to server
        self.m = TBS(address=address, authkey=authkey)
        self.m.connect()

    def __enter__(self):
        # setup ConnectionTradingBotManager to TBM
        self.conn_tbm.setup(
            self.m.get_reader_tbm(self.id),
            self.m.get_writer_tbm(self.id)
        )
        # get state of server process
        self.p_state = self.m.get_state()
        # get queue to notify new client to TBM
        self.q_to_tbm = self.m.get_queue_cli_to_tbm()
        # notify new client to TBM
        self.q_to_tbm.put((self.id, 'up'),)

    def __exit__(self, exc_type, exc_value, exc_tb):
        # shutdown ConnectionTradingBotManager to TBM
        self.q_to_tbm.put((self.id, 'down'),)
        if self.conn_tbm.state == 'up':
            self.conn_tbm.shutdown(msg=exc_type)

        self.logger.info('closed connection to TBM and thread')

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
        _ClientBot.__init__(self, address=address, authkey=authkey)

    def __enter__(self):
        self.conn_tbm = ConnTradingBotManager(self.id)
        super(_ClientStrategyBot, self).__enter__()
        # get queue to send orders to OrdersManager
        self.q_ord = self.m.get_queue_orders()
        # get queue to send orders to PerformanceManager
        self.q_tpm = self.m.get_queue_sb_to_tpm()

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
        fees = self.p_state._getvalue()['fees']
        if fees:

            return float(fees[self._handler[order_type]][pair]['fee'])

        else:

            return 0.0

    def get_available_volume(self, ccy):
        """ Get the current available volume in balance following a currency.

        Parameters
        ----------
        ccy : str
            Currency pair.

        Returns
        -------
        float
            Available volume for the corresponding currency.

        """
        return float(self.p_state._getvalue()['balance'][ccy])


class _ClientOrdersManager(_ClientBot):
    """ Base class for an OrderdManager object. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a client object and connect to TradingBotServer. """
        self.id = 0
        _ClientBot.__init__(self, address=address, authkey=authkey)
        self.conn_tbm = ConnTradingBotManager(self.id)

    def __enter__(self):
        super(_ClientOrdersManager, self).__enter__()
        # get queue to receive orders from StrategyBot
        self.q_ord = self.m.get_queue_orders()


class _ClientPerformanceManager(_ClientBot):
    """ Base class for a TradingPerformanceManager object. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        self.id = -1
        _ClientBot.__init__(self, address=address, authkey=authkey)
        self.conn_tbm = ConnTradingBotManager(self.id)

    def __enter__(self):
        super(_ClientPerformanceManager, self).__enter__()
        # get queue to receive orders from StrategyBot
        self.q_tpm = self.m.get_queue_sb_to_tpm()


class _ClientCLI(_ClientBot):
    """ Base class for Command Line Interface. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        self.id = -2
        super(_ClientCLI, self).__init__(address=address, authkey=authkey)
        self.conn_tbm = ConnTradingBotManager(self.id)
