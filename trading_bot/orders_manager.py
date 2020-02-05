#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-04-29 23:42:09
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-05 10:23:27

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
from trading_bot._exceptions import MissingOrderError

__all__ = ['OrdersManager']

"""
TODO list:
    - New method : set history orders
    - New method : get available funds
    - New method : verify integrity of new orders
    - New method : (future) split orders for a better scalability
"""


class _Orders(dict):
    _waiting, _open, _close = [], [], []

    def __init__(self, *args, **kwargs):
        super(_Orders, self).__init__(*args, **kwargs)
        self._set_state()

    def __setitem__(self, key, value):
        print('set {}'.format(key))
        dict.__setitem__(self, key, value)
        self._add_state(key, value)

    def __delitem__(self, key):
        print('del {}'.format(key))
        dict.__delitem__(self, key)
        self._del_state(key)

    def __repr__(self):
        return str({
            'waiting': self._waiting,
            'open': self._open,
            'close': self._close
        }) + '\n' + dict.__repr__(self)

    def pop(self, key):
        print('pop {}'.format(key))
        self._del_state(key)

        return dict.pop(self, key)

    def popitem(self):
        key, value = dict.popitem(self)
        self._del_state(key)

        return key, value

    def update(self, *args, **kwargs):
        dict.update(self, *args, **kwargs)
        self._reset_state()

    def get_first(self):
        ordered_list = self.get_ordered_list()

        return ordered_list[0]

    def get_ordered_list(self):
        return self._waiting + self._open + self._close

    def pop_first(self):
        key = self.get_first()

        return key, self.pop(key)

    def _set_state(self):
        for key, value in self.items():
            self._add_state(key, value)

    def _reset_state(self):
        self._waiting, self._open, self._close = [], [], []
        self._set_state()

    def _add_state(self, key, value):
        if value.get('state') is None:
            self._waiting.append(key)

        elif value['state'] == 'open':
            self._open.append(key)

        elif value['state'] == 'close':
            self._close.append(key)

        else:
            raise ValueError('unknown state {}'.format(value['state']))

    def _del_state(self, key):
        if key in self._waiting:
            self._waiting.remove(key)

        elif key in self._open:
            self._open.remove(key)

        elif key in self._close:
            self._close.remove(key)

        else:
            raise ValueError('unknown id_order: {}'.format(key))


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
    orders = _Orders()

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
        path_log : str
            Path where API key and secret are saved.
        exchange : str, optional
            Name of the exchange (default is `'kraken'`).

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
                kwrds = {
                    'input': kwargs,
                    'open_out': [],
                    'close_out': [],
                    'request_out': [],
                    'state': None
                }

            return id_order, kwrds

        elif self.orders:

            return self.orders.pop_first()

        return None, {}

    def loop(self):
        """ Run a loop until condition is false. """
        self.logger.info('loop | wait orders')
        last_order = 0
        for id_order, kwrds in self:
            if id_order is None:
                # DO SOMETHING ELSE (e.g. display results_manager)
                pass

            elif kwrds.get('state') is None:
                self.post_order(id_order, kwrds)
                last_order = time.time()

            elif kwrds['state'] == 'open':
                self.check_post_order(id_order, kwrds)

            elif kwrds['state'] == 'close':
                self.logger.debug('loop | remove {}'.format(id_order))
                # TODO : save order
                # TODO : update results_manager

            else:

                raise ValueError('unknown state: {}'.format(kwrds['state']))

            txt = time.strftime('%y-%m-%d %H:%M:%S') + ' | Last order was '
            txt += str_time(int(time.time() - last_order)) + ' ago'
            print(txt, end='\r')
            time.sleep(0.01)

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

    with open('./trading_bot_manager/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    path_log = '/home/arthur/Strategies/Data_Server/Untitled_Document2.txt'
    om = OrdersManager()  # path_log)
    with om('kraken', path_log):
        om.loop()
