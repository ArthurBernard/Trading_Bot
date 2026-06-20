#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 09:58:03
# @Last modified by: ArthurBernard
# @Last modified time: 2020-03-28 11:20:10

""" Base object for trading bot server. """

# Built-in packages
import logging
from multiprocessing.managers import BaseManager
from multiprocessing import Pipe
import os
from queue import Queue
from threading import Thread
import time

# Third party packages

# Local packages
from trading_bot._connection import (ConnOrderManager, ConnStrategyBot,
                                     ConnPerformanceManager, ConnCLI)
from trading_bot._containers import ConnDict
from trading_bot._exceptions import ConnRefused


class TradingBotServer(BaseManager):
    """ Trading bot server. """

    pass


class _TradingBotManager:
    """ Base class of trading bot manager. """

    conn_sb = ConnDict()
    conn_om = ConnOrderManager()
    conn_tpm = ConnPerformanceManager()
    conn_cli = ConnCLI()

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize the trading bot manager. """
        self.logger = logging.getLogger(__name__)
        self.logger.info(
            'PID: {} | PPID: {}'.format(os.getpid(), os.getppid())
        )

        # Set queue for orders
        self.q_ord = Queue()
        TradingBotServer.register(
            'get_queue_orders',
            callable=lambda: self.q_ord
        )

        # Set queue for performances
        self.q_sb_to_tpm = Queue()
        TradingBotServer.register(
            'get_queue_sb_to_tpm',
            callable=lambda: self.q_sb_to_tpm,
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
        self.state = {'stop': True, 'balance': {}, 'fees': {}}
        TradingBotServer.register('get_state', callable=lambda: self.state)

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
        self.logger.debug('stop is propageted')
        time.sleep(1)
        self.s.stop_event.set()
        self.server_thread.join()
        self.client_thread.join()

    def is_stop(self):
        return self.state['stop']

    def set_stop(self, is_stop):
        self.state['stop'] = is_stop

    def set_server(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a server connection. """
        self.m = TradingBotServer(address=address, authkey=authkey)
        self.s = self.m.get_server()
        self.logger.info('server started')
        self.state['stop'] = False
        self.s.serve_forever()
        self.logger.info('server stopped')

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

        elif _id == -1:
            self.conn_tpm._set_reader(r)

        elif _id == -2:
            self.conn_cli._set_reader(r)

        else:
            if _id not in self.conn_sb.keys():
                self.conn_sb.append(ConnStrategyBot(_id))

            if self.conn_sb[_id].state != 'up':
                self.conn_sb[_id]._set_reader(r)

            else:
                self.logger.error('writer to {} already exists'.format(_id))

                raise ConnRefused(_id, msg='already connected to TBM')

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

        elif _id == -1:
            self.conn_tpm._set_writer(w)

        elif _id == -2:
            self.conn_cli._set_writer(w)

        else:
            if _id not in self.conn_sb.keys():
                self.conn_sb.append(ConnStrategyBot(_id))

            if self.conn_sb[_id].state != 'up':
                self.conn_sb[_id]._set_writer(w)

            else:
                self.logger.error('reader to {} already exists'.format(_id))

                raise ConnRefused(_id, msg='already connected to TBM')

        return r
