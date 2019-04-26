#!/usr/bin/env python
# coding: utf-8

# Import built-in packages
from pickle import Pickler, Unpickler
import logging

# Import external packages
from requests import HTTPError

# Import internal packages
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
    """ Class to set and manage orders. Verify the intigrity of the new orders
    with past orders and suffisant funds.

    An id order is a signed integer smaller than 32-bit, three last number
    correspond to the id strategy and the other numbers correspond to an id
    user. The id user is in fact an id time, it corresponding at the number
    of minutes since a starting point saved in the file 'id_timestamp'. The
    file 'id_timestamp' will be reset every almost three years.

    Methods
    -------
    order(**kwargs)
        Request an order (with krakenex in first order).
    decode_id_order(id_order)
        Takes an id order and returns the corresponding id strategy and
        timestamp.
    get_query_order(id_order)
        Return status of a specified order or position.
    # TODO : cancel orders/position if too far of mid
    # TODO : replace limit order/position
    # TODO : market order/position if time is over

    Attributs
    ---------
    id_strat : int (signed and max 32-bit)
        Number to link an order with a strategy.
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

        self.logger = logging.getLogger('strat_man.' + __name__)

        if exchange.lower() == 'kraken':
            self.K = KrakenClient()
            self.K.load_key(path_log)

        else:
            self.logger.error('Exchange "{}" not allowed'.format(exchange))

            raise ValueError(exchange + ' not allowed.')

    def order(self, **kwargs):
        """ Request an order following defined parameters.

        /! To verify ConnectionResetError exception. /!

        Parameters
        ----------
        kwargs : dict
            Parameters for ordering, refer to API documentation of the
            plateform used.

        Return
        ------
        out : json
            Output of the request.

        """
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

            # TO DEBUG
            # try:
            #    # Set infos
            #    out['result']['userref'] = id_order
            #    out['result']['timestamp'] = now(self.frequency)

            # except KeyError:
            #    self.logger.error('KeyError:\n\nout: {}\n\nkwargs:{}\n'.format(
            #        str(out), str(kwargs)
            #    ), exc_info=True)

            # TO DEBUG
            # self.logger.debug('Output orders: {}'.format(out))

        except Exception as e:

            if e in [HTTPError]:
                self.logger.error('Catching the following error: {}'.format(e))

                query = self.get_query_order(id_order)

                if query['status'] not in ['open', 'close', 'pending']:
                    out = self.order(**kwargs)

            else:
                self.logger.error('UNKNOWN ERROR', exc_info=True)

                raise e

        # Check if order is ordered correctly
        query = self.get_query_order(id_order)

        # TO DEBUG
        self.logger.debug(str(query))

        if kwargs['validate']:

            return out

        elif query['status'] not in ['open', 'close', 'pending']:
            out = self.order(**kwargs)

        return out

    def _set_id_order(self):
        """ Set an identifier for an order according with the strategy
        reference, time and optional id parameters.

        Returns
        -------
        id_order : int (signed and 32-bit)
            Number to link an order with strategy, time and other.

        """
        id_user = self._get_id_user()
        id_order = int(str(id_user) + str(self.id_strat))

        return id_order

    def _get_id_user(self):
        """ Get id user in function of a time starting point (in minutes).
        Time starting point restart from 0 every almost 3 years.

        Returns
        -------
        id_user : int (signed and less than 32-bit)
            Number to link an order with a time.

        """
        try:

            with open('id_timestamp', 'rb') as f:
                TS = Unpickler(f).load()

        except FileNotFoundError:
            TS = 0

        id_user = (now(1) - TS) // 60

        if id_user > self.id_max // 1000:

            with open('id_timestamp', 'wb') as f:
                Pickler(f).dump(now(1))

            id_user = self._get_id_user()

        return id_user

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
        out : list
            Orders answere.

        """
        out = []

        # Don't move
        if self.current_pos == signal:

            return [self.set_output(kwargs)]

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
        """ Cut short position """
        if self.current_pos < 0:
            # Set leverage to cut short
            leverage = kwargs.pop('leverage')
            leverage = 2 if leverage is None else leverage + 1

            # Set volume to cut short
            kwargs['volume'] = self.current_vol

            # Query order
            out = self.order(leverage=leverage, **kwargs)

            # Set current volume and position
            self.current_vol = 0.
            self.current_pos = 0
            out['result']['current_volume'] = 0.
            out['result']['current_position'] = 0

        else:
            out = self.set_output(kwargs)

        return out

    def set_long(self, signal, **kwargs):
        """ Set long order """
        if signal > 0:
            out = self.order(**kwargs)

            # Set current volume
            self.current_vol = kwargs['volume']
            self.current_pos = signal
            out['result']['current_volume'] = self.current_vol
            out['result']['current_position'] = signal

        else:
            out = self.set_output(kwargs)

        return out

    def cut_long(self, signal, **kwargs):
        """ Cut long position """
        if self.current_pos > 0:
            # Set volume to cut long
            kwargs['volume'] = self.current_vol
            out = self.order(**kwargs)

            # Set current volume
            self.current_vol = 0.
            self.current_pos = 0
            out['result']['current_volume'] = 0.
            out['result']['current_position'] = 0

        else:
            out = self.set_output(kwargs)

        return out

    def set_short(self, signal, **kwargs):
        """ Set short order """
        if signal < 0:
            # Set leverage to short
            leverage = kwargs.pop('leverage')
            leverage = 2 if leverage is None else leverage + 1
            out = self.order(leverage=leverage, **kwargs)

            # Set current volume
            self.current_vol = kwargs['volume']
            self.current_pos = signal
            out['result']['current_volume'] = self.current_vol
            out['result']['current_position'] = signal

        else:
            out = self.set_output(kwargs)

        return out

    def set_output(self, kwargs):
        """ Set output when no orders query """
        # TODO : /!\ get execution price for market order /!\
        out = {
            'result': {
                'timestamp': now(self.frequency),
                'current_volume': self.current_vol,
                'current_position': self.current_pos,
                'descr': None,
            }
        }
        if kwargs['ordertype'] == 'limit':
            out['price'] = kwargs['price']

        elif kwargs['ordertype'] == 'market':
            out['price'] = get_close(kwargs['pair'])

        else:
            raise ValueError(
                'Unknown order type: {}'.format(kwargs['ordertype'])
            )

        return out

    def decode_id_order(self, id_order):
        """ From an id order decode the time (in minute) and the strategy
        corresponding to the order.

        Parameters
        ----------
        id_order : int (signed and 32-bit)
            Number to link an order with strategy, time and other.

        Returns
        -------
        TS : int (signed)
            Timestamp at the passing order.
        id_strat : int (unsigned and 32-bit)
            Number to link an order with a strategy.

        """
        id_user = id_order // 1000
        id_strat = id_order % 1000

        with open('id_timestamp', 'rb') as f:
            TS = Unpickler(f).load() + id_user * 60

        return TS, id_strat

    def get_query_order(self, id_order):
        """ Return query order of a specified id order.

        Parameters
        ----------
        id_order : int (signed and 32-bit)
            Number to link an order with strategy, time and other.

        Returns
        -------
        str
            Query of specified order.

        """
        try:
            ans = self.K.query_private(
                'OpenOrders',
                trades=True,
                userref=id_order,
            )

            return ans['result']

        except Exception as e:

            if e in [HTTPError]:
                self.logger.error('Catching the following error: {}'.format(e))

                return self.get_status_order(id_order)

            else:
                self.logger.error(
                    'UNKNOWN ERROR : {}\nAnswere of query: {}'.format(
                        type(e),
                        ans
                    ),
                    exc_info=True
                )

                raise e
