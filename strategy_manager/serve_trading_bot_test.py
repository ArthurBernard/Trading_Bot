#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 09:58:03
# @Last modified by: arthur
# @Last modified time: 2020-01-27 10:22:44

""" Test main trading bot object. """

# Built-in packages
from multiprocessing.managers import BaseManager
from queue import Queue

# Third party packages

# Local packages


class TradingBotManager(BaseManager):
    """ Trading Bot Manager object. """

    pass


def set_server_tradingbot(address= ('', 50000), authkey=b'tradingbot'):
    """ Set the trading bot server. """
    q = Queue()
    TradingBotManager.register('get_queue', callable=lambda: q)
    m = TradingBotManager(address=address, authkey=authkey)
    s = m.get_server()
    s.serve_forever()


if __name__ == '__main__':
    q = Queue()
    TradingBotManager.register('get_queue', callable=lambda: q)
    m = TradingBotManager(address=('', 50000), authkey=b'tradingbot')
    s = m.get_server()
    s.serve_forever()
