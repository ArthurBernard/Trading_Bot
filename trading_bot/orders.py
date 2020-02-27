#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-06 11:57:48
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-27 11:27:32

""" Module with different Order objects.

Each order inherits from _BasisOrder object, and each order object has
specified `update` method and some specific attributes.

"""

# Built-in packages
import time
import logging

# Third party packages

# Local packages
from trading_bot._exceptions import OrderError, OrderStatusError
from trading_bot.data_requests import get_ask, get_bid, get_close

__all__ = ['OrderSL', 'OrderBestLimit']


class _BasisOrder:
    """ Basis order object.

    Methods
    -------
    execute
    cancel
    get_closed
    get_open
    get_result_exec
    set_client_API
    update

    Attributes
    ----------
    id : int
        ID of the order (32-bit).
    exchange_client : object inherits from ExchangeClient
        API client to send private requests.
    status : {None, 'open', 'canceled', 'closed'}
        Status of the order.
        - If None, then the order have never been executed.
        - If 'open', then the order was sent.
        - If 'closed', then the order was sent and all the initial volume was
        executed.
        - If 'canceled', then the order was canceled and only the quantity of
        `vol_exec` was executed.
    input : dict
        Parameters of the order to sent.
    volume : float
        Initial volume to order.
    type : {'buy', 'sell'}
        Type of the order.
    vol_exec : float
        Current volume executed.
    pair : str
        Code corresponding to the pair of the order.
    price : float or 'market'
        Price to sent to the order.
    state : dict
        Last state of the order (ansewered by the exchange API).
    history : list of dict
        Historic of all anseweres by the exchange API.
    tol : float
            Tolerance's threshold for non-executed volume. Default is 0.1%.
    result_exec : dict
        {txid : list, price_exec: float, vol_exec: float, fee: float,
        feeb: float, feeq: float, cost: float, start_time: int}
    time_force : int
        Timestamp after which the order will be forced to be executed at the
        market price.

    """

    def __init__(self, id, input={}, tol=0.001, time_force=None, info={}):
        """ Initialize an order object.

        Parameters
        ----------
        id : int
            ID of the order (32-bit signed integer).
        input : dict, optional
            Input to request order.
        tol : float, optional
            Tolerance's threshold for non-executed volume. Default is 0.1%.
        time_force : int, optional
            Number of seconds to wait before force execute to the market price.
            If set to None, then never force execute to the market price
            (actually it will be forced in more than 300years). Default is
            None.
        info : dict, optional
            Any additional informations (usefull to compute strategy
            performance). Default is an empty dict.

        """
        self.info = info
        # self.fee = None
        self.result_exec = {
            'txid': [],
            'price_exec': 0,
            'vol_exec': 0,
            'fee': 0,
            'feeq': 0,
            'feeb': 0,
            'cost': 0,
            'start_time': int(time.time())
        }
        self.logger = logging.getLogger('Order-' + str(id))
        self.id = id
        self.input = input
        self.tol = tol
        self.time_force = time_force if time_force is not None else 1e10

        self.volume = input['volume']
        self.type = input['type']
        self.vol_exec = 0.
        self.price_exec = 0.
        self.pair = input['pair']

        if input['ordertype'] == 'market':
            self.price = 'market'

        else:
            self.price = input['price']

        if self.input['leverage'] == 1:
            self.input['leverage'] = None

        self.time_force += self.result_exec['start_time']
        self.state = None
        self.status = None
        self.hist = []
        self.logger.debug('initialized')

    def __repr__(self):
        """ Represent the order. """
        return ("[Order ID {self.id}] - status: {self.status}, type: "
                "{self.type}, pair: {self.pair}, price: {self.price}, volume: "
                "{self.volume}, vol_exec: {self.vol_exec}".format(self=self))

    def execute(self):
        """ Execute the order. """
        if self.status is None or self.status == 'canceled':
            self._last = int(time.time())
            ans = self._request('AddOrder', userref=self.id, **self.input)
            self._update_status('open')
            self.state = ans
            if self.input.get('validate'):
                self.logger.debug('order is validate mode')
                self._update_status('closed')
                if self.price == 'market':
                    self.price = get_close(self.pair)
                    self.logger.debug('set close price {}'.format(self.price))

        else:
            raise OrderStatusError(self, 'execute')

    def cancel(self):
        """ Cancel the order. """
        if self.status == 'open':
            ans = self._request('CancelOrder', txid=self.id)
            if ans['count'] == 0:

                raise OrderError(self, 'no order canceled')

            else:
                self._update_status('canceled')

                return ans

        else:

            raise OrderStatusError(self, 'cancel')

    def get_closed(self, start):
        """ Get the closed orders corresponding to the ID.

        Parameters
        ----------
        start : int
            Timestamp from which requested closed orders.

        Returns
        -------
        dict
            Closed orders.

        """
        return self._request('ClosedOrders', userref=self.id, start=start)

    def get_open(self):
        """ Get the open orders corresponding to the ID.

        Returns
        -------
        dict
            Open orders.

        """
        return self._request('OpenOrders', userref=self.id)

    def check_vol_exec(self):
        """ Check if the volume has been executed and set corresponding status.

        If the executed volume is equal (or almost equal) to the volume
        attribute then the status is set to 'closed'. Otherwise, the status
        stays 'open'. Nevertheless, if the executed volume exceeds the volume
        then an exception is raised.

        Notes
        -----
        If a small part of the volume is not executed (less than `tol`%) then
        the status is set to 'closed'.

        """
        if self.status is None:

            raise OrderStatusError(self, 'check_vol_exec')

        # FIXME : some issues may occurs if an order is executed between the
        # call of get_closed() and the setting of _last attribute
        ans = self.get_closed(start=self._last)
        self._get_vol_exec(ans['closed'])

        if self.vol_exec == self.volume:
            self._update_status('closed')
            self._get_result_exec(ans['closed'])

        elif 1 - self.vol_exec / self.volume < self.tol:
            self._update_status('closed')
            self._get_result_exec(ans['closed'])
            not_exec_vol = 1 - self.vol_exec / self.volume
            self.logger.warning("{:.6%} of the volume was not executed but is "
                                "less than tolerance's threshold {:%}"
                                "".format(not_exec_vol, self.tol))

        elif self.vol_exec > self.volume:

            raise OrderError(self, msg_prefix='too many volume executed: ')

        else:
            self.logger.debug('check executed vol: exVol={}, newVol={}'.format(
                self.input['volume'], self.volume - self.vol_exec
            ))
            self.input['volume'] = self.volume - self.vol_exec

    def set_client_API(self, exchange_client, call_counter=None):
        """ Set the client API to private requests.

        Parameters
        ----------
        exchange_client : ExchangeClient
            Object to connect with the client API of the exchange.
        call_counter : CallCounter, optional
            Object that calls itself at each private request, and if call rate
            limit is exceeded then the object waits a few seconds. By default
            the object is None, so it never waits.

        """
        self.exchange_client = exchange_client
        if call_counter is None:
            self.call_counter = lambda x: None

        else:
            self.call_counter = call_counter

    def _get_vol_exec(self, closed_orders):
        self._last = int(time.time())
        for v in closed_orders.values():
            self.vol_exec += v['vol_exec']

    def _request(self, method, **kwargs):
        if 'exchange_client' not in self.__dict__.keys():

            raise AttributeError(
                'you must setup an exchange_client, see set_client_API'
            )

        self.call_counter(method)
        ans = self.exchange_client.query_private(method, **kwargs)
        self.hist += [ans]
        self.logger.debug('send {} | answere: {}'.format(method, ans))

        return ans

    def _update_status(self, status):
        if self.status == status:
            self.logger.error('cant update status, already {}'.format(status))
            raise OrderStatusError(self, status)

        elif self.status == 'closed':
            self.logger.error(
                'cant {} status if closed'.format(status)
            )
            raise OrderStatusError(self, status)

        elif status not in ['closed', 'open', 'canceled']:
            self.logger.error('{} status not allowed'.format(status))
            raise OrderStatusError(self, status)

        elif status in ['canceled', 'closed'] and self.status != 'open':
            self.logger.error(
                'cant {} status if not open'.format(status)
            )
            raise OrderStatusError(self, status)

        else:
            self.logger.debug(
                'from {} status to {}'.format(self.status, status)
            )
            self.status = status

    def get_result_exec(self):
        """ Get execution information (price, fees, etc.).

        Store the execution information in the attribute `result_exec`.

        """
        if self.status != 'closed':

            raise OrderStatusError(self, 'get_result_exec')

        ans = self.get_closed(start=self.result_exec['start_time'])
        self._get_result_exec(ans['closed'])
        self.logger.debug('execution info: {}'.format(self.result_exec))

    def _get_result_exec(self, closed_orders):
        self.result_exec['txid'] = list(closed_orders.keys())
        for v in closed_orders.values():
            if 'viqc' in v['oflags']:
                v['price'] = 1 / v['price']
                v['vol_exec'] *= v['price']
                v['cost'] *= v['price']
                v['oflags'].remove('viqc')

            self.result_exec['vol_exec'] += v['vol_exec']
            self.result_exec['price_exec'] += v['price'] * v['vol_exec']
            self.result_exec['fee'] += v['fee']
            self.result_exec['cost'] += v['cost']

            if 'fciq' in v['oflags']:
                self.result_exec['feeq'] += v['fee']

            elif 'fcib' in v['oflags']:
                self.result_exec['feeb'] += v['fee'] / v['price']

        if self.result_exec['vol_exec'] > 0.:
            self.result_exec['price_exec'] /= self.result_exec['vol_exec']


class OrderSL(_BasisOrder):
    """ Submit and Leave order object.

    The order is added at a limit price and it leave until it is executed.

    Methods
    -------
    execute
    cancel
    get_open
    get_closed
    check_vol_exec
    update

    Attributes
    ----------
    id : int
        ID of the order (32-bit).
    exchange_client : object inherits from ExchangeClient
        API client to send private requests.
    status : {None, 'open', 'canceled', 'closed'}
        Status of the order.
    input : dict
        Parameters of the order to sent.
    volume : float
        Initial volume to order.
    type : {'buy', 'sell'}
        Type of the order.
    vol_exec : float
        Current volume executed.
    pair : str
        Code corresponding to the pair of the order.
    price : float or 'market'
        Initial price sent to the order.
    state : dict
        Last state of the order (ansewered by the exchange API).
    history : list of dict
        Historic of all anseweres by the exchange API.
    tol : float
        Tolerance's threshold for non-executed volume. Default is 0.1%.
    result_exec : dict
        {txid : list, price_exec: float, vol_exec: float, fee: float,
        feeb: float, feeq: float, cost: float, start_time: int}
    time_force : int
        Timestamp after which the order will be forced to be executed at the
        market price.

    """

    def update(self):
        """ Check if the volume has been executed and set corresponding status.

        If the executed volume is equal (or almost equal) to the volume
        attribute then the status is set to 'closed'. Otherwise, the status
        stays 'open'. Nevertheless, if the executed volume exceeds the volume
        then an exception is raised.

        Notes
        -----
        If a small part of the volume is not executed (less than `tol`%) then
        the status is set to 'closed'.

        """
        if self.status is not 'open':

            raise OrderStatusError(self, 'update')

        if time.time() > self.time_force:
            if self.get_open():
                self.cancel()

            self.check_vol_exec()

            if self.status != 'closed':
                if time.time() > self.time_force:
                    self.input['type'] = 'market'
                    self.input.remove('price')

                self.execute()

        elif not self.get_open():
            self.check_vol_exec()


class OrderBestLimit(_BasisOrder):
    """ Set order at the best limit price and update the price regularly.

    Methods
    -------
    execute
    cancel
    get_open
    get_closed
    check_vol_exec
    update

    Attributes
    ----------
    id : int
        ID of the order (32-bit).
    exchange_client : object inherits from ExchangeClient
        API client to send private requests.
    status : {None, 'open', 'canceled', 'closed'}
        Status of the order.
    input : dict
        Parameters of the order to sent.
    volume : float
        Initial volume to order.
    type : {'buy', 'sell'}
        Type of the order.
    vol_exec : float
        Current volume executed.
    pair : str
        Code corresponding to the pair of the order.
    price : float or 'market'
        Initial price sent to the order.
    state : dict
        Last state of the order (ansewered by the exchange API).
    history : list of dict
        Historic of all anseweres by the exchange API.
    tol : float
        Tolerance's threshold for non-executed volume. Default is 0.1%.
    result_exec : dict
        {txid : list, price_exec: float, vol_exec: float, fee: float,
        feeb: float, feeq: float, cost: float, start_time: int}
    time_force : int
        Timestamp after which the order will be forced to be executed at the
        market price.

    """

    _handler_best = {
        'buy': get_bid,
        'sell': get_ask,
    }

    def update(self, price='best'):
        """ Cancel the open or pending order and add a new order.

        The new order fill the non-executed volume and it add at the specified
        price, if price is None the order will be at the market price or if the
        is "best" then the order will be add at the best ask/bid price.

        If orders are already executed, then set status to 'closed' and get
        execution restuls.

        Parameters
        ----------
        price : float or {'best', 'market'}, optional
            Price to add the new order. If 'market' then the order will be at
            market price, if 'best' then the order will be at the best ask/bid
            price. Default is 'best'.

        """
        if self.status is None or self.status == 'closed':

            raise OrderStatusError(self, 'replace')

        if self.get_open():
            self.cancel()

        self.check_vol_exec()

        if self.status != 'closed':
            if price == 'market' or time.time() > self.time_force:
                self.input['type'] = 'market'
                self.input.remove('price')

            elif price == 'best':
                self.input['price'] = self._handler_best[self.type](self.pair)

            else:
                self.input['price'] = price

            self.execute()
