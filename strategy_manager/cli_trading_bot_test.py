#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 10:07:30
# @Last modified by: arthur
# @Last modified time: 2020-01-27 10:23:50

""" Test client Trading Bot Manager object. """

# Built-in packages
from serve_trading_bot_test import TradingBotManager as TBM
import os

# Third party packages

# Local packages

if __name__ == '__main__':
    TBM.register('get_queue')
    m = TBM(address=('', 50000), authkey=b'tradingbot')
    m.connect()
    q = m.get_queue()
    while True:
        i = input()
        if i.lower() == 'pid':
            print(os.getpid())

        elif i:
            q.put(i)

        elif not q.empty():
            print(q.get())
