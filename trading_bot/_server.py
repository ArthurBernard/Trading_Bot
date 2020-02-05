#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 09:58:03
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-05 10:32:53

""" Base object for trading bot server. """

# Built-in packages
from multiprocessing.managers import BaseManager, BaseProxy
from multiprocessing import Pipe
from queue import Queue
import os

# Third party packages

# Local packages


class TradingBotServer(BaseManager):
    """ Trading bot server. """

    pass


class _TradingBotManager:
    """ Base class of trading bot manager. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize the trading bot manager. """
        print('Module {}: process ID is {} and parent PID is {}'.format(
            __name__, os.getpid(), os.getppid()
        ))

        # Set queue for orders
        self.q_ord = Queue()
        TradingBotServer.register(
            'get_queue_orders',
            callable=lambda: self.q_ord
        )

        # Set pipe with order manager
        self.r_om, w = Pipe(duplex=False)
        r, self.w_om = Pipe(duplex=False)
        TradingBotServer.register('get_writer_tbm', callable=lambda: w)
        TradingBotServer.register('get_reader_tbm', callable=lambda: r)

        # manager = Manager()
        # Set a proxy to share a state
        # self.stop = True  # manager.Value(bool, True)  # False
        # self.stop = BoolProxy()
        self.state = {
            'stop': True,
        }
        TradingBotServer.register(
            'get_state',
            callable=lambda: self.state,
            # proxytype=BoolProxy,
        )

        # Set proxy to share fees dictionary
        self.fees = {}  # manager.dict()  # {}
        # self.fees = DictProxy()
        TradingBotServer.register(
            'get_proxy_fees',
            callable=lambda: self.fees,
            # proxytype=DictProxy,
        )


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
