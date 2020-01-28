#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 10:07:30
# @Last modified by: ArthurBernard
# @Last modified time: 2020-01-28 15:35:57

""" Test.

Client to run a strategy and to send orders to execute to a server. Server is
a Trading Bot Manager object.

"""

# Built-in packages
import os
import sys
import time

# Third party packages

# Local packages
from serve_trading_bot_test import TradingBotServer as TBS
from orders_manager import SetOrder
# from manager import StrategyManager

# TODO:
#    - Client Strategy Manager object
#    - Client Orders Manager object


class _Client:
    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a client object. """
        TBS.register('get_queue_orders')
        self.m = TBS(address=address, authkey=authkey)
        self.m.connect()
        self.q_ord = self.m.get_queue_orders()


class StrategyManagerClient(_Client):
    """ Main object to load data, compute signals and execute orders.

    Methods
    -------
    get_order_params(data)
        Computes and returns signal strategy and additional parameters to set
        order.
    __call__(*args, **kwargs)
        Set parameters to pass into function' strategy.
    set_iso_vol(series, target_vol=0.2, leverage=1., period=252, half_life=11)
        Computes and returns iso-volatility coefficient.

    Attributes
    ----------
    frequency : int
        Number of seconds between two samples.
    underlying : str
        Name of the underlying or list of data needed.
    _get_order_params : function
        Function to get signal strategy and additional parameters to set order.

    """

    def send_order(self, *args, **kwargs):
        """ Send an order to the order manager object. """
        self.q_ord.put((args, kwargs),)


# TODO : Client order manager:
#          - Asynchronous object
#          - Compute balance
#          - Check available balance
#          - Count limit API requests
#          - Singleton


class OrderManagerClient(SetOrder, _Client):
    """ Order manager client. """

    def __init__(self, *args, address=('', 50000), authkey=b'tradingbot',
                 **kwargs):
        _Client.__init__(self, address=address, authkey=authkey)
        SetOrder.__init__(self, *args, **kwargs)

    def wait_orders(self):
        """ Run get orders. """
        self.wait = True
        while self.wait:
            self.get_orders()

    def get_orders(self):
        """ Get an order from a strategy manager. """
        if not self.q_ord.empty():
            o = self.q_ord.get()
            if isinstance(o, str) and o.lower() == 'stop':
                self.wait = False

            else:
                self.order()

        else:
            # time.time(5)
            return


if __name__ == '__main__':
    if sys.argv[1].lower() == 'order-manager':
        cli = OrderManagerClient(0, '', )
        for i in range(5):
            cli.wait_orders()

    else:
        cli = StrategyManagerClient()
        for i in range(5):
            cli.send_order(i, i2=i ** 2)
