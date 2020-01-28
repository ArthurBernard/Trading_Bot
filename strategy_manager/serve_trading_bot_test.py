#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 09:58:03
# @Last modified by: ArthurBernard
# @Last modified time: 2020-01-28 15:11:46

""" Test.

Server to receive orders to execute from clients. Clients are the trading
strategies running.

"""

# Built-in packages
from multiprocessing.managers import BaseManager
from threading import Thread
from queue import Queue
import time
import os

# Third party packages

# Local packages


class TradingBotServer(BaseManager):
    """ Trading bot server. """

    pass


class TradingBotManager:
    """ Trading Bot Manager object. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot', s=9):
        """ Initialize Trading Bot Manager object. """
        self.q_ord = Queue()
        TradingBotServer.register(
            'get_queue_orders',
            callable=lambda: self.q_ord
        )
        print('Current PID is {}'.format(os.getpid()))
        self.t = time.time()
        server_thread = Thread(
            target=self.set_server,
            kwargs={'address': address, 'authkey': authkey}
        )
        bot_thread = Thread(target=self.runtime, args=(s,))
        server_thread.start()
        bot_thread.start()
        # server_thread.join()
        # bot_thread.join()
        print('Initialization is finished.')

    def set_server(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a server connection. """
        self.m = TradingBotServer(address=address, authkey=authkey)
        self.s = self.m.get_server()
        print('Server is initialized.')
        self.s.serve_forever()
        print('Server is stopped.')

    def runtime(self, s=9):
        """ Do something. """
        # TODO : Run OrderManagerClient object
        # TODO : Run all StrategyManagerClient objects
        print('Start to do something')
        while time.time() - self.t < s:
            print('{:.1f} sec.'.format(time.time() - self.t), end='\r')
            time.sleep(0.1)

        print('\nEnd to do something')
        self.s.stop_event.set()


def start_tradingbotserver(address=('', 50000), authkey=b'tradingbot'):
    """ Set the trading bot server. """
    q_orders = Queue()
    TradingBotServer.register('get_queue_orders', callable=lambda: q_orders)
    # q2 = Queue()
    # TradingBotManager.register('get_queue2', callable=lambda: q2)
    m = TradingBotServer(address=address, authkey=authkey)
    s = m.get_server()
    s.serve_forever()


if __name__ == '__main__':
    # start_tradingbotmanager()
    tbm = TradingBotManager(s=20)
    # tbm.run()
