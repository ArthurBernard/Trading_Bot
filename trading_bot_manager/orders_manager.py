#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-04-29 23:42:09
# @Last modified by: ArthurBernard
# @Last modified time: 2020-01-31 14:09:09

""" Client to manage orders execution. """

# Built-in packages
from pickle import Pickler, Unpickler
import logging
import time
from os import getpid, getppid

# External packages
import numpy as np

# Internal packages
from tools.time_tools import str_time, now
# from strategy_manager.API_kraken import KrakenClient
from data_requests import get_close
from API_kraken import KrakenClient
from _client import _OrderManagerClient
# from _server import TradingBotServer as TBS

__all__ = ['OrdersManager']

"""
TODO list:
    - New method : set history orders
    - New method : get available funds
    - New method : verify integrity of new orders
    - New method : (future) split orders for a better scalability
"""


class OrdersManager(_OrderManagerClient):
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
    # TODO : replace limit order/position
    # TODO : market order/position if time is over
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

    def __init__(self, path_log, exchange='kraken', address=('', 50000),
                 authkey=b'tradingbot'):
        """ Set the order class.

        Parameters
        ----------
        path_log : str
            Path where API key and secret are saved.
        current_pos : int, {1 : long, 0 : None, -1 : short}
            Current position of the account.
        current_vol : float
            Current volume position.
        exchange : str, optional
            Name of the exchange (default is `'kraken'`).
        frequency : int, optional
            Frequency to round timestamp.

        """
        # Set client and connect to the trading bot server
        _OrderManagerClient.__init__(self, address=address, authkey=authkey)

        self.logger = logging.getLogger('trad_bot.' + __name__)
        #self.logger.info('Initialize OrdersManager | Current PID is '
        #                 '{} and Parent PID is {}'.format(getpid()), getppid())
        self.logger.info('Initialize OrdersManager | Current PID is {} and '
                         'Parent PID is {}'.format(getpid(), getppid()))
        self.id_max = 2147483647
        self.path = path_log
        self.exchange = exchange
        self.t = self.start = int(time.time())
        self.call_counter = 0

        self.K = KrakenClient()
        self.K.load_key(path_log)
        self.logger.debug('Exchange client loaded.')
        self.get_fees()
        self.get_balance()

    def start_loop(self, condition=True):
        """ Run a loop until condition is false. """
        self.logger.info('Starting to wait orders.')
        last_order = 0
        while condition:
            if not self.q_ord.empty():
                id_strat, kwrds = self.q_ord.get()
                self.logger.debug(
                    'Get order | Strat {} | Params {}'.format(id_strat, kwrds)
                )
                result = self.set_order(id_strat, **kwrds)
                self.logger.debug('Result: {}'.format(result))
                last_order = time.time()

            else:
                # DO SOMETHING ELSE (e.g. results_manager)
                pass

            txt = time.strftime('%y-%m-%d %H:%M:%S') + ' | Last order was '
            txt += str_time(int(time.time() - last_order)) + ' ago'
            print(txt, end='\r')
            time.sleep(0.01)

            if self.is_stop():
                break

        self.logger.info('OrdersManager stopped.')

    def set_order(self, id_strat, **kwargs):
        """ Request an order following defined parameters.

        /! To verify ConnectionResetError exception. /!

        Parameters
        ----------
        id_strat : int
            Identifier of the strategy (between 0 and 99).
        kwargs : dict
            Parameters for ordering, refer to API documentation of the
            plateform used.

        Return
        ------
        dict
            Result of output of the request.

        """
        id_order = self._set_id_order(id_strat)

        if kwargs['leverage'] == 1:
            kwargs['leverage'] = None

        # TODO : Append a method to verify if the volume is available.
        try:
            # Send order
            out = self.K.query_private(
                'AddOrder',
                userref=id_order,
                timeout=30,
                **kwargs
            )
            self.call_count(pt=0)
            self.logger.info(out['descr']['order'])
            txid = out['txid']

        except (NameError, KeyError) as e:
            self.logger.error('Output error: {}'.format(type(e)))
            txid = 0

        except Exception as e:
            self.logger.error('Unknown error: {}'.format(type(e)),
                              exc_info=True)

            raise e

        # Verify if order is posted
        # time.sleep(1)
        post_order = self.verify_post_order(id_order)
        if not post_order and not kwargs['validate']:
            self.logger.error('ORDER NOT SENT. \n\nBot retry to send order\n')
            time.sleep(1)

            return self.order(id_strat=id_strat, **kwargs)

        return self._set_result_output(txid, id_order, **kwargs)

    def verify_post_order(self, id_order):
        """ Verify if an order is well posted.

        Parameters
        ----------
        id_order : int
            User reference of the order to verify.

        Returns
        -------
        bool
            Return true if order is posted else false.

        """
        open_order = self.K.query_private('OpenOrders', userref=id_order)
        self.call_count()

        if open_order['open']:

            return True

        closed_order = self.K.query_private(
            'ClosedOrders',
            userref=id_order,
            start=self.start
        )
        self.call_count()

        if closed_order['closed']:

            return True

        self.logger.info('Order not verified.')

        return False

    def _set_result_output(self, txid, id_order, **kwargs):
        """ Add informations to output of query order. """
        pair = kwargs['pair']
        ordertype = kwargs['ordertype']
        result = {
            'txid': txid,
            'userref': id_order,
            'type': kwargs['type'],
            'volume': kwargs['volume'],
            'pair': pair,
            'ordertype': ordertype,
            'leverage': kwargs['leverage'],
            'timestamp': int(time.time()),  # now(self.frequency),
            'fee': float(self.fees[self._handler[ordertype]][pair]['fee'])  # self._get_fees(kwargs['pair'], kwargs['ordertype']),
        }
        if ordertype == 'market' and kwargs['validate']:
            # Get the last price
            result['price'] = get_close(pair)

        elif ordertype == 'market' and not kwargs['validate']:
            # TODO : verify if get the exection market price
            closed_order = self.K.query_private('ClosedOrders',
                                                userref=id_order,
                                                start=self.start)  # ['result']
            txids = closed_order['closed'].keys()
            result['price'] = np.mean([
                closed_order['closed'][i]['price'] for i in txids
            ])
            self.logger.debug('Get execution price is not yet verified')

        elif ordertype == 'limit':
            result['price'] = kwargs['price']

        return result

    def _set_output(self, kwargs):
        """ Set output when no orders query. """
        result = {
            'timestamp': now(self.frequency),
            'current_volume': self.current_vol,
            'current_position': self.current_pos,
            'fee': self._get_fees(kwargs['pair'], kwargs['ordertype']),
            'descr': None,
        }
        if kwargs['ordertype'] == 'limit':
            result['price'] = kwargs['price']

        elif kwargs['ordertype'] == 'market':
            result['price'] = get_close(kwargs['pair'])

        else:
            raise ValueError(
                'Unknown order type: {}'.format(kwargs['ordertype'])
            )

        return result

    def get_fees(self):
        if self.exchange.lower() == 'kraken':
            self.fees = self.K.query_private(
                'TradeVolume',
                pair='all'
            )
            self.call_count()
            self.logger.debug('Got fees from the exchange.')

        else:
            self.logger.error('Exchange {} not allowed'.format(self.exchange))

            raise ValueError(self.exchange + ' not allowed.')

        self.w_tbm.send({'fees': self.fees})
        self.logger.debug('Sent fees to TradingBotManager.')

    def get_balance(self):
        if self.exchange.lower() == 'kraken':
            self.balance = self.K.query_private('Balance')
            self.call_count()
            self.logger.debug('Got balance : {}'.format(self.balance))

        else:
            self.logger.error('Exchange {} not allowed'.format(self.exchange))

            raise ValueError(self.exchange + ' not allowed.')

        self.w_tbm.send({'balance': self.balance})
        self.logger.debug('Sent balance to TradingBotManager.')

    def _set_id_order(self, id_strat):
        """ Set an unique order identifier.

        Parameters
        ----------
        id_strat : int
            Identifier of the strategy (between 0 and 99).

        Returns
        -------
        id_order : int (signed and 32-bit)
            Number to identify an order and link it with a strategy.

        """
        try:

            with open('id_order.dat', 'rb') as f:
                id_order = Unpickler(f).load()

        except FileNotFoundError:
            id_order = 0

        id_order += 1
        if id_order > self.id_max // 100:
            id_order = 0

        with open('id_order.dat', 'wb') as f:
            Pickler(f).dump(id_order)

        if id_strat < 10:
            id_strat = '0' + str(id_strat)

        return int(str(id_order) + str(id_strat))

    def call_count(self, pt=1, discount=2, max_call=20):
        self.call_counter += pt
        t = int(time.time())
        self.call_counter -= (t - self.t) // discount
        self.call_counter = max(self.call_counter, 0)
        self.t = t
        self.logger.debug('Call count: {}'.format(self.call_counter))
        if self.call_counter >= max_call:
            self.logger.info('Max call exceeded: {}'.format(self.call_counter))
            time.sleep(self.call_counter - max_call + 1)


if __name__ == '__main__':

    import logging.config
    import yaml

    with open('./trading_bot_manager/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    path_log = '/home/arthur/Strategies/Data_Server/Untitled_Document2.txt'
    OM = OrdersManager(path_log)
    OM.start_loop()
