#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 09:58:03
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-20 23:27:43

""" Base object for trading bot server. """

# Built-in packages
import logging
from multiprocessing.managers import BaseManager, BaseProxy
from multiprocessing import Pipe
import os
from queue import Queue
from threading import Thread
import time

# Third party packages

# Local packages
from trading_bot._connection import Connection, ConnDict


class TradingBotServer(BaseManager):
    """ Trading bot server. """

    pass


class _TradingBotManager:
    """ Base class of trading bot manager. """
    conn_sb = ConnDict()
    conn_om = Connection(0, name='order_manager')

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize the trading bot manager. """
        self.logger = logging.getLogger(__name__)
        self.logger.info(
            'Module {}: process ID is {} and parent PID is {}'.format(
                __name__, os.getpid(), os.getppid()
            )
        )

        # Set queue for orders
        self.q_ord = Queue()
        TradingBotServer.register(
            'get_queue_orders',
            callable=lambda: self.q_ord
        )

        # Set queue for strategies
        self.q_from_cli = Queue()
        TradingBotServer.register(
            'get_queue_cli_to_tbm',
            callable=lambda: self.q_from_cli
        )

        # Set pipe for client bot
        TradingBotServer.register('get_writer_tbm', callable=self.get_writer)
        TradingBotServer.register('get_reader_tbm', callable=self.get_reader)

        # Set a proxy to share a state
        self.state = {'stop': True}
        TradingBotServer.register('get_state', callable=lambda: self.state)

        # Set proxy to share fees dictionary
        self.fees = {}
        TradingBotServer.register('get_proxy_fees', callable=lambda: self.fees)

        # Set client and server threads
        self.server_thread = Thread(
            target=self.set_server,
            kwargs={'address': address, 'authkey': authkey}
        )

    def __enter__(self):
        """ Enter to TradingBotManager. """
        self.server_thread.start()

    def __exit__(self, exc_type, exc_value, exc_tb):
        """ Exit from TradingBotManager. """
        self.state['stop'] = True
        self.logger.debug('exit | stop propageted')
        time.sleep(1)
        self.s.stop_event.set()
        self.server_thread.join()
        self.client_thread.join()

    def is_stop(self):
        return self.state['stop']

    def set_server(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a server connection. """
        self.m = TradingBotServer(address=address, authkey=authkey)
        self.s = self.m.get_server()
        self.logger.info('set_server | started')
        # print(self.stop)
        self.state['stop'] = False
        self.s.serve_forever()
        self.logger.info('set_server | stopped')

    def get_writer(self, _id):
        """ Set a pipe, returns the writer and store the reader.

        Parameters
        ----------
        _id : int
            ID of the client bot.

        Returns
        -------
        Connection object
            Writer side of a pipe, from the client to the TradingBotManager.

        """
        r, w = Pipe(duplex=False)
        if _id == 0:
            self.conn_om._set_reader(r)

        else:
            if _id not in self.conn_sb:
                self.conn_sb.append(Connection(_id))

            self.conn_sb[_id]._set_reader(r)

        return w

    def get_reader(self, _id):
        """ Set a pipe, returns the reader and store the writer.

        Parameters
        ----------
        _id : int
            ID of the client bot.

        Returns
        -------
        Connection object
            Reader side of a pipe, from the client to the TradingBotManager.

        """
        r, w = Pipe(duplex=False)
        if _id == 0:
            self.conn_om._set_writer(w)

        else:
            if _id not in self.conn_sb:
                self.conn_sb.append(Connection(_id))

            self.conn_sb[_id]._set_writer(w)

        return r


class BoolProxy(BaseProxy):
    value = None

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value


class DictProxy(BaseProxy):
    value = {}

    def get_value(self, key):
        return self.value[key]

    def set_value(self, key, value):
        self.value[key] = value
