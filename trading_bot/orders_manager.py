#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-04-29 23:42:09
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-25 12:08:14

""" Client to manage orders execution. """

# Built-in packages
import logging
from multiprocessing import Pipe
from os import getpid, getppid
from pickle import Pickler, Unpickler
import time

# External packages
import numpy as np

# Internal packages
from trading_bot._client import _ClientOrdersManager
from trading_bot._exceptions import MissingOrderError, OrderError
from trading_bot._containers import OrderDict
from trading_bot.data_requests import get_close
from trading_bot.exchanges.API_kraken import KrakenClient
from trading_bot.tools.call_counters import KrakenCallCounter
from trading_bot.tools.time_tools import str_time

__all__ = ['OrdersManager']

# TODO list:
#    - New method : set history orders
#    - New method : get available funds
#    - New method : verify integrity of new orders
#    - New method : (future) split orders for a better scalability


class OrdersManager(_ClientOrdersManager):
    """ Client to set and manage orders.

    Verify the intigrity of the new orders with past orders and suffisant
    funds.
    An id order is a signed integer smaller than 32-bit, three last number
    correspond to the id strategy and the other numbers correspond to an id
    user. The id user is in fact an id time, it corresponding at the number
    of minutes since a starting point saved in the file 'id_timestamp'. The
    file 'id_timestamp' will be reset every almost three years.

    Methods
    -------
    order(**kwargs)
        Request an order (with krakenex in first order).
    get_query_order(id_order)
        Return status of a specified order or position.
    # TODO : cancel orders/position if too far of mid
    # TODO : Singleton
    # TODO : Asynchronous methods
    # TODO : get_balance
    # TODO : load order config

    Attributs
    ---------
    id_max : int
        Number max for an id_order (32-bit).
    path : str
        Path where API key and secret are saved.
    K : API
        Object to query orders on Kraken exchange.

    """

    _handler_client = {
        'kraken': KrakenClient,
    }
    _handler_call_counters = {
        'kraken': KrakenCallCounter('intermediate'),
    }
    orders = OrderDict()

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Set the order class.

        Parameters
        ----------
        address :
        authkey :

        """
        # Set client and connect to the trading bot server
        _ClientOrdersManager.__init__(self, address=address, authkey=authkey)
        self.logger = logging.getLogger(__name__)
        self.logger.info('init | PID: {} PPID: {}'.format(getpid(), getppid()))

        self.id_max = 2147483647
        self.start = int(time.time())

    def __call__(self, exchange, path_log):
        """ Set parameters of order manager.

        Parameters
        ----------
        exchange : str, optional
            Name of the exchange (default is `'kraken'`).
        path_log : str
            Path where API key and secret are saved.

        Returns
        -------
        OrdersManager
            Object to manage orders.

        """
        if exchange.lower() in self._handler_client.keys():
            self.K = self._handler_client[exchange.lower()]()

        else:
            raise ValueError('exchange {} not supported'.format(exchange))

        self.path = path_log
        self.exchange = exchange
        self.call_counter = self._handler_call_counters.get(exchange.lower())

        self.K.load_key(path_log)
        self.logger.debug('call | {} client loaded'.format(exchange))

        return self

    def __enter__(self):
        """ Enter to context manager. """
        super(OrdersManager, self).__enter__()
        # TODO : load config and data
        self.logger.info('enter | Load configuration')
        # TODO : load orders to verify
        self.logger.debug('enter | order: {}'.format(self.orders))
        # Setup fees and balance
        self.get_fees()
        self.get_balance()

        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        """ Exit from context manager. """
        self.logger.debug('exit | order: {}'.format(self.orders))
        # TODO : save config and data
        self.logger.info('exit | Save configuration')
        if exc_type is not None:
            self.logger.error('exit | {}: {}\n{}'.format(
                exc_type, exc_value, exc_tb
            ))

        self.logger.info('exit | end')
        super(OrdersManager, self).__exit__(exc_type, exc_value, exc_tb)

    def __iter__(self):
        """ Iterate until server stop. """
        self.logger.info('iter | Starting to wait orders')

        return self

    def __next__(self):
        """ Next method.

        Returns
        -------
        int
           Identifier of the order. If None then there is no order to manage.
        dict
           Dictionary containing input (dict), output (dict or list) and state
           (string).

        """
        if self.is_stop():

            raise StopIteration

        elif not self.q_ord.empty():
            order = self.q_ord.get()
            order.set_client_API(self.K, call_counter=self.call_counter)

            return order

        elif self.orders:
            id_order = self.orders.get_first()

            return self.orders.pop(id_order)

        return None

    def loop(self):
        """ Run a loop until TradingBotServer closed. """
        self.logger.info('loop | start to wait orders')
        # TODO : get last order
        last_order = 0
        for order in self:
            if order is None:
                # DO SOMETHING ELSE (e.g. display results_manager)
                txt = time.strftime('%y-%m-%d %H:%M:%S') + ' | Last order was '
                txt += str_time(int(time.time() - last_order)) + ' ago'
                print(txt, end='\r')
                time.sleep(0.01)

                continue

            elif order.status is None:
                self.logger.debug('execute {}'.format(order))
                order.execute()
                last_order = time.time()
                self.orders.append(order)

            elif order.status == 'open' or order.status == 'canceled':
                order.update()

                self.orders.append(order)

            elif order.status == 'canceled':
                # TODO: check vol, replace order
                self.logger.debug('replace {}'.format(order))
                order.replace('best')
                self.orders.append(order)

            elif order.status == 'closed':
                # res = self._set_result(order)
                # self.conn_tbm.send({'closed_order': res})
                self.conn_tbm.send(('order', order),)
                # TODO : save order object
                # TODO : update results_manager
                self.logger.debug('remove {}'.format(order))

            else:

                raise OrderError(order, 'unknown state')

        self.logger.info('OrdersManager stopped.')

    def get_fees(self):
        """ Load current fees. """
        self.fees = self.K.query_private(
            'TradeVolume',
            pair='all'
        )
        self.call_counter('TradeVolume')
        self.logger.debug('get_fees | fees are loaded')

        self.conn_tbm.send(('fees', self.fees),)
        self.logger.debug('get_fees | fees are sent to TradingBotManager')

    def get_balance(self):
        """ Load current balance. """
        self.balance = self.K.query_private('Balance')
        self.call_counter('Balance')
        self.logger.debug('get_balance | Loaded {}'.format(self.balance))

        self.conn_tbm.send(('balance', self.balance),)
        self.logger.debug('get_balance | Sent balance to TradingBotManager')

    def _get_id_strat(self, id_order, n=3):
        return int(str(id_order)[-n:])

    def _set_result(self, order):
        """ Add informations to output of query order.

        Returns
        -------
        dict
            {'txid': list, 'price_exec': float, 'vol_exec': float,
            'fee': float, 'feeq': float, 'feeb': float, 'cost': float,
            'start_time': int, 'userref': int, 'type': str, 'volume', float,
            'price': float, 'pair': str, 'ordertype': str, 'leverage': int,
            'end_time': int, 'fee_pct': float, 'strat_id': int}.

        """
        pair = order.pair
        ordertype = order.input['ordertype']
        result = order.get_result_exec()
        result.update({
            'userref': order.id,
            'type': order.type,
            'price': order.price,
            'volume': order.volume,
            'pair': pair,
            'ordertype': ordertype,
            'leverage': order.input['leverage'],
            'end_time': int(time.time()),
            'fee_pct': float(self.fees[self._handler[ordertype]][pair]['fee']),
            'strat_id': self._get_id_strat(order.id),
        })

        return result


if __name__ == '__main__':

    import logging.config
    import yaml

    with open('./trading_bot/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    path_log = '/home/arthur/Strategies/Data_Server/Untitled_Document2.txt'
    om = OrdersManager()
    with om('kraken', path_log):
        om.loop()
