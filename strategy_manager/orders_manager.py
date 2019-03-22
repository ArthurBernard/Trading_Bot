#!/usr/bin/env python
# coding: utf-8

# Import built-in packages
from pickle import Pickler, Unpickler
import time

# Import external package(s)
from krakenex import API
import numpy as np

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

    def __init__(self, id_strat, path_log, current_pos=0, exchange='kraken'):
        """ Set the order class.

        Parameters
        ----------
        id_strat : int (unsigned and 32-bit)
            Number to link an order with a strategy.
        path : str
            Path where API key and secret are saved.
        current_pos : int, {1 : long, 0 : None, -1 : short}
            Current position of the account.
        exchange : str, optional
            Name of the exchange (default is `'kraken'`).

        """
        self.id_strat = id_strat
        self.id_max = 2147483647
        self.path = path_log
        self.current_pos = current_pos
        if exchange.lower() == 'kraken':
            # Use krakenex API while I am lazy to do myself
            self.K = API()
            self.K.load_key(path_log)
        else:
            raise ValueError(str(exchange) + ' not allowed.')

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
                'AddOrder', {'userref': id_order, **kwargs}
            )

            # TO DEBUG
            print(out)

        except Error as e:
            print(str(type(e)), str(e), ' error !')
            if e in [timeout]:
                query = self.get_status_order(id_order)
                if query['status'] not in ['open', 'close', 'pending']:
                    out = self.order(**kwargs)
            elif e in [ConnectionResetError]:
                self.K = API()
                self.K.load_key(self.path)
                out = self.order(**kwargs)
            else:
                raise ValueError('Error unknown ', type(e), e)
        # Check if order is ordered correctly
        query = self.get_query_order(id_order)

        # TO DEBUG
        print(query)

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
            f = open('id_timestamp', 'rb')
            TS = Unpickler(f).load()
            f.close()
        except FileNotFoundError:
            TS = 0
        now = int(time.time())
        id_user = (now - TS) // 60
        if id_user > self.id_max // 1000:
            f = open('id_timestamp', 'wb')
            Pickler(f).dump(now)
            f.close()
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
            return out
        # Up move
        elif self.current_pos <= 0 and signal >= 0:
            kwargs['type'] = 'buy'
            out += [self.cut_short(signal, **kwargs.copy())]
            out += [self.set_long(signal, **kwargs)]
        # Down move
        elif self.current_pos >= 0 and signal <= 0:
            kwargs['type'] = 'sell'
            out += [self.cut_long(signal, **kwargs.copy())]
            out += [self.set_short(signal, **kwargs)]
        # Update current position
        self.current_pos = signal
        return out

    def cut_short(self, signal, **kwargs):
        """ Cut short position """
        if self.current_pos < 0:
            leverage = kwargs.pop('leverage')
            leverage = 2 if leverage is None else leverage + 1
            return self.order(leverage=leverage, **kwargs)

    def set_long(self, signal, **kwargs):
        """ Set long order """
        if signal > 0:
            return self.order(**kwargs)

    def cut_long(self, signal, **kwargs):
        """ Cut long position """
        if self.current_pos > 0:
            return self.order(**kwargs)

    def set_short(self, signal, **kwargs):
        """ Set short order """
        if signal < 0:
            leverage = kwargs.pop('leverage')
            leverage = 2 if leverage is None else leverage + 1
            return self.order(leverage=leverage, **kwargs)

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
        f = open('id_timestamp', 'rb')
        TS = Unpickler(f).load() + id_user * 60
        f.close()
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
                'QueryOrders', {'trades': True, 'userref': id_order}
            )
        except Error as e:
            print(str(type(e)), str(e), ' error !')
            if e in [timeout]:
                return self.get_status_order(id_user)
            elif e in [ConnectionResetError]:
                self.K = API()
                self.K.load_key(self.path)
                return self.get_status_order(id_user)
            else:
                raise ValueError('Error unknown: ', type(e), e)
        return ans
