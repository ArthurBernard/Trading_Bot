#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-04-29 23:42:09
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-14 14:55:50

""" Client to manage orders execution. """

# Built-in packages
from pickle import Pickler, Unpickler
import logging
import time
from os import getpid, getppid

# External packages
import numpy as np

# Internal packages
from trading_bot.tools.time_tools import str_time  # , now
from trading_bot.data_requests import get_close
from trading_bot.API_kraken import KrakenClient
from trading_bot._client import _OrderManagerClient
from trading_bot._exceptions import MissingOrderError, OrderError
from trading_bot._order import Order, OrderDict

__all__ = ['OrdersManager']

# TODO list:
#    - New method : set history orders
#    - New method : get available funds
#    - New method : verify integrity of new orders
#    - New method : (future) split orders for a better scalability


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

    _handler_client = {
        'kraken': KrakenClient,
    }
    orders = OrderDict()  # _Orders()

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Set the order class.

        Parameters
        ----------
        address :
        authkey :

        """
        # Set client and connect to the trading bot server
        _OrderManagerClient.__init__(self, address=address, authkey=authkey)
        self.logger = logging.getLogger('OrdersManager.' + __name__)
        self.logger.info('init | PID: {} PPID: {}'.format(getpid(), getppid()))

        self.id_max = 2147483647
        self.t = self.start = int(time.time())
        self.call_counter = 0

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
        self.path = path_log
        self.exchange = exchange

        if exchange.lower() in self._handler_client.keys():
            self.K = self._handler_client[exchange.lower()]()

        else:
            raise ValueError('Exchange {} not supported'.format(exchange))

        self.K.load_key(path_log)
        self.logger.debug('call | {} client loaded'.format(exchange))
        self.get_fees()
        self.get_balance()

        return self

    def __enter__(self):
        """ Enter to context manager. """
        # TODO : load config and data
        self.logger.info('enter | Load configuration')
        # TODO : load orders to verify
        self.logger.debug('enter | order: {}'.format(self.orders))

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
            id_strat, kwargs = self.q_ord.get()
            self.logger.debug('next | {}: {}'.format(id_strat, kwargs))
            if kwargs.get('userref'):
                id_order = kwargs.pop('userref')

            else:
                id_order = self._set_id_order(id_strat)
                # kwrds = {
                #    'input': kwargs,
                #    'open_out': [],
                #    'close_out': [],
                #    'request_out': [],
                #    'state': None
                # }

            return Order(id_order, self.K, input=kwargs)

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
                order.execute()
                last_order = time.time()
                self.orders.append(order)

            elif order.status == 'open':
                if order.get_open():
                    order.replace('best')

                else:
                    order.check_vol_exec()
                    if order.status != 'closed':

                        raise OrderError(order, 'missing volume')

                self.orders.append(order)

            elif order.status == 'canceled':
                # TODO: check vol, replace order
                order.replace('best')
                self.orders.append(order)

            elif order.status == 'closed':
                self.logger.debug('loop | remove {}'.format(order.id))
                # TODO : save order
                # TODO : update results_manager
                pass

            else:

                raise OrderError(order, 'unknown state')

            self.logger('loop | {}'.format(order))

        self.logger.info('OrdersManager stopped.')

    def set_order(self, **kwargs):
        """ Request an order following defined parameters.

        /! To verify ConnectionResetError exception. /!

        Parameters
        ----------
        kwargs : dict
            Parameters for ordering, refer to API documentation of the
            plateform used.

        Return
        ------
        dict
            Result of output of the request.

        """
        if kwargs['leverage'] == 1:
            kwargs['leverage'] = None

        # TODO : Append a method to verify if the volume is available.
        try:
            # Send order
            output = self.K.query_private('AddOrder', **kwargs)
            self.call_count(pt=0)
            self.logger.info(output['descr']['order'])
            if kwargs['validate']:
                self.logger.info('set_order | Validating order is True')
                output['txid'] = 0

        except Exception as e:
            self.logger.error('set_order | Unknown error: {}'.format(type(e)),
                              exc_info=True)

            raise e

        return output

    def post_order(self, id_order, kwrds):
        """ Post an order. """
        self.logger.debug('post | {}: {}'.format(id_order, kwrds))
        out = self.set_order(userref=id_order, **kwrds['input'])
        kwrds['request_out'] += out if isinstance(out, list) else [out]
        kwrds['state'] = 'open'
        self.orders[id_order] = kwrds
        self.logger.debug('post | output: {}'.format(out))

    def check_post_order(self, id_order, kwrds):
        """ Verify if an order was posted.

        Parameters
        ----------
        id_order : int
            User reference of the order to verify.
        kwrds : dict
            Information about the order.

        """
        open_ord = self.K.query_private('OpenOrders', userref=id_order)
        print(open_ord)
        self.call_count()
        if open_ord['open']:
            # TODO : cancel order, update price and post_order
            self.logger.debug('check | {} always open'.format(id_order))
            kwrds['open_out'] += open_ord['open']
            self.orders[id_order] = kwrds

        else:
            close_ord = self.K.query_private(
                'ClosedOrders',
                userref=id_order,
                start=self.start
            )
            self.call_count()
            if close_ord['closed']:
                self.logger.debug('check | {} closed'.format(id_order))
                kwrds['closed_out'] += close_ord['closed']
                kwrds['state'] = 'close'
                self.orders[id_order] = kwrds

            elif kwrds['input'].get('validate'):
                self.logger.warning('check | {} validating'.format(id_order))

            else:
                self.logger.error('check | {} missing'.format(id_order))

                raise MissingOrderError(id_order, params=kwrds)

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
            'timestamp': int(time.time()),
            'fee': float(self.fees[self._handler[ordertype]][pair]['fee'])
        }
        if ordertype == 'market' and kwargs['validate']:
            # Get the last price
            result['price'] = get_close(pair)

        elif ordertype == 'market' and not kwargs['validate']:
            # TODO : verify if get the exection market price
            closed_order = self.K.query_private('ClosedOrders',
                                                userref=id_order,
                                                start=self.start)
            txids = closed_order['closed'].keys()
            result['price'] = np.mean([
                closed_order['closed'][i]['price'] for i in txids
            ])
            self.logger.debug('Get execution price is not yet verified')

        elif ordertype == 'limit':
            result['price'] = kwargs['price']

        return result

    def get_fees(self):
        """ Load current fees. """
        self.fees = self.K.query_private(
            'TradeVolume',
            pair='all'
        )
        self.call_count()
        self.logger.debug('get_fees | fees are loaded')

        self.w_tbm.send({'fees': self.fees})
        self.logger.debug('get_fees | fees are sent to TradingBotManager')

    def get_balance(self):
        """ Load current balance. """
        self.balance = self.K.query_private('Balance')
        self.call_count()
        self.logger.debug('get_balance | Loaded {}'.format(self.balance))

        self.w_tbm.send({'balance': self.balance})
        self.logger.debug('get_balance | Sent balance to TradingBotManager')

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
        """ Count the number of requests done and wait if exceed the max rate.

        Parameters
        ----------
        pt: int
            Number to increase the call rate counter.
        discount: int
            Number of seconds to decrease of one the call rate counter.
        max_call: int
            Max call rate counter.

        """
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

    with open('./trading_bot/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    path_log = '/home/arthur/Strategies/Data_Server/Untitled_Document2.txt'
    om = OrdersManager()
    with om('kraken', path_log):
        om.loop()
