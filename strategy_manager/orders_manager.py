#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-04-29 23:42:09
# @Last modified by: ArthurBernard
# @Last modified time: 2019-09-07 11:20:16

""" Manage orders execution. """

# Built-in packages
from pickle import Pickler, Unpickler
import logging
import time

# External packages
import numpy as np

# Internal packages
from strategy_manager.tools.time_tools import now
from strategy_manager.API_kraken import KrakenClient
from strategy_manager.data_requests import get_close

__all__ = ['SetOrder']

"""
TODO list:
    - New method : set history orders
    - New method : get available funds
    - New method : verify integrity of new orders
    - New method : (future) split orders for a better scalability
"""


class SetOrder:
    """ Class to set and manage orders.

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

    Attributs
    ---------
    id_strat : int (signed and max 32-bit)
        Number to identify an order and link it with a strategy.
    id_max : int
        Number max for an id_order (32-bit).
    path : str
        Path where API key and secret are saved.
    K : API
        Object to query orders on Kraken exchange.
    current_pos : float
        The currently position, {-1: short, 0: neutral, 1: long}.

    """

    def __init__(self, id_strat, path_log, current_pos=0, current_vol=0,
                 exchange='kraken', frequency=1):
        """ Set the order class.

        Parameters
        ----------
        id_strat : int (unsigned and 32-bit)
            Number to link an order with a strategy.
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
        self.id_strat = id_strat
        self.id_max = 2147483647
        self.path = path_log
        self.current_pos = current_pos
        self.current_vol = current_vol
        self.frequency = frequency
        self.start = int(time.time())

        self.logger = logging.getLogger('strat_man.' + __name__)

        if exchange.lower() == 'kraken':
            self.K = KrakenClient()
            self.K.load_key(path_log)
            self.fees_dict = self.K.query_private(
                'TradeVolume',
                pair='all'
            )

        else:
            self.logger.error('Exchange "{}" not allowed'.format(exchange))

            raise ValueError(exchange + ' not allowed.')

    def order(self, id_order=None, **kwargs):
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
        if id_order is None:
            id_order = self._set_id_order()

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
        time.sleep(1)
        post_order = self.verify_post_order(id_order)
        if not post_order and not kwargs['validate']:
            self.logger.info('Bot will retry to send order.')
            time.sleep(1)

            return self.order(id_order=id_order, **kwargs)

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

        if open_order['open']:

            return True

        closed_order = self.K.query_private('ClosedOrders',
                                            userref=id_order, start=self.start)

        if closed_order['closed']:

            return True

        self.logger.info('Order not verified.')

        return False

    def _set_result_output(self, txid, id_order, **kwargs):
        """ Add informations to output of query order. """
        result = {
            'txid': txid,
            'userref': id_order,
            'type': kwargs['type'],
            'volume': kwargs['volume'],
            'pair': kwargs['pair'],
            'ordertype': kwargs['ordertype'],
            'leverage': kwargs['leverage'],
            'timestamp': now(self.frequency),
            'fee': self._get_fees(kwargs['pair'], kwargs['ordertype']),
        }
        if kwargs['ordertype'] == 'market' and kwargs['validate']:
            # Get the last price
            result['price'] = get_close(kwargs['pair'])

        elif kwargs['ordertype'] == 'market' and not kwargs['validate']:
            # TODO : verify if get the exection market price
            closed_order = self.K.query_private('ClosedOrders',
                                                userref=id_order,
                                                start=self.start)  # ['result']
            txids = closed_order['closed'].keys()
            result['price'] = np.mean([
                closed_order['closed'][i]['price'] for i in txids
            ])
            self.logger.debug('Get execution price is not yet verified')

        elif kwargs['ordertype'] == 'limit':
            result['price'] = kwargs['price']

        return result

    def _set_result_output2(self, out, id_order, **kwargs):
        """ Add informations to output of query order. """
        # Set infos
        out['userref'] = id_order
        out['timestamp'] = now(self.frequency)
        out['fee'] = self._get_fees(kwargs['pair'], kwargs['ordertype'])

        if kwargs['ordertype'] == 'market' and kwargs['validate']:
            # Get the last price
            out['price'] = get_close(kwargs['pair'])

        elif kwargs['ordertype'] == 'market' and not kwargs['validate']:
            # TODO : verify if get the exection market price
            closed_order = self.K.query_private('ClosedOrders',
                                                userref=id_order,
                                                start=self.start)  # ['result']
            txid = out['txid']
            out['price'] = closed_order['closed'][txid]['price']
            self.logger.debug('Get execution price is not yet verified')

        return out

    def set_order(self, signal, **kwargs):
        """ Set parameters to order.

        Parameters
        ----------
        signal : int, {1 : long, 0 : None, -1 : short}
            Signal of strategy.
        kwargs : dict
            Parameters for ordering.

        Returns
        -------
        list
            Orders outputs.

        """
        out = []

        # Don't move
        if self.current_pos == signal:

            return [self._set_output(kwargs)]

        # Up move
        elif self.current_pos <= 0. and signal >= 0:
            kwargs['type'] = 'buy'
            out += [self.cut_short(signal, **kwargs.copy())]
            out += [self.set_long(signal, **kwargs.copy())]

        # Down move
        elif self.current_pos >= 0. and signal <= 0:
            kwargs['type'] = 'sell'
            out += [self.cut_long(signal, **kwargs.copy())]
            out += [self.set_short(signal, **kwargs.copy())]

        return out

    def cut_short(self, signal, **kwargs):
        """ Cut short position. """
        if self.current_pos < 0:
            # Set leverage to cut short
            leverage = kwargs.pop('leverage')
            leverage = 2 if leverage is None else leverage + 1

            # Set volume to cut short
            kwargs['volume'] = self.current_vol

            # Query order
            result = self.order(leverage=leverage, **kwargs)

            # Set current volume and position
            self.current_vol = 0.
            self.current_pos = 0
            result['current_volume'] = 0.
            result['current_position'] = 0

        else:
            result = self._set_output(kwargs)

        return result

    def set_long(self, signal, **kwargs):
        """ Set long order. """
        if signal > 0:
            result = self.order(**kwargs)

            # Set current volume
            self.current_vol = kwargs['volume']
            self.current_pos = signal
            result['current_volume'] = self.current_vol
            result['current_position'] = signal

        else:
            result = self._set_output(kwargs)

        return result

    def cut_long(self, signal, **kwargs):
        """ Cut long position. """
        if self.current_pos > 0:
            # Set volume to cut long
            kwargs['volume'] = self.current_vol
            result = self.order(**kwargs)

            # Set current volume
            self.current_vol = 0.
            self.current_pos = 0
            result['current_volume'] = 0.
            result['current_position'] = 0

        else:
            result = self._set_output(kwargs)

        return result

    def set_short(self, signal, **kwargs):
        """ Set short order. """
        if signal < 0:
            # Set leverage to short
            leverage = kwargs.pop('leverage')
            leverage = 2 if leverage is None else leverage + 1
            result = self.order(leverage=leverage, **kwargs)

            # Set current volume
            self.current_vol = kwargs['volume']
            self.current_pos = signal
            result['current_volume'] = self.current_vol
            result['current_position'] = signal

        else:
            result = self._set_output(kwargs)

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

    def _get_fees(self, pair, order_type):
        """ Get current fees of order.

        Parameters
        ----------
        pair : str
            Symbol of the currency pair.
        order_type : str
            Type of order.

        Returns
        -------
        float
            Fees of specified pair and order type.

        """
        if order_type == 'market':

            return float(self.fees_dict['fees'][pair]['fee'])

        elif order_type == 'limit':

            return float(self.fees_dict['fees_maker'][pair]['fee'])

        else:

            raise ValueError('Unknown order type: {}'.format(order_type))

    def _set_id_order(self):
        """ Set an unique order identifier.

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

        return int(str(id_order) + str(self.id_strat))
