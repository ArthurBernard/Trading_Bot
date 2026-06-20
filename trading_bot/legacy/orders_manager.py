#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-04-29 23:42:09
# @Last modified by: ArthurBernard
# @Last modified time: 2020-05-09 17:02:22

""" Client to manage orders execution. """

# Built-in packages
import logging
import time

# External packages
# import numpy as np

# Internal packages
from trading_bot._client import _ClientOrdersManager
from trading_bot._containers import OrderDict
from trading_bot._exceptions import OrderError, InsufficientFundsError
from trading_bot.exchanges.API_kraken import KrakenClient
from trading_bot.order.io import update_hist_orders
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
        self.logger = logging.getLogger('orders_manager')
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
        self.logger.debug('{} client API loaded'.format(exchange))

        return self

    def __enter__(self):
        """ Enter to context manager. """
        super(OrdersManager, self).__enter__()
        # TODO : load config and data
        self.logger.info('Load configuration')
        # Load unexecuted orders
        try:
            self.orders._load('./strategies/', 'unexecuted_orders', ext='.dat')
            self.logger.debug('load unexecuted orders: {}'.format(self.orders))

        except FileNotFoundError:

            pass

        # Setup fees and balance
        self.get_fees()
        self.get_balance()

        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        """ Exit from context manager. """
        # Save unexecuted orders
        self.logger.debug('save unexecuted orders: {}'.format(self.orders))
        self.orders._save('./strategies/', 'unexecuted_orders', ext='.dat')
        # TODO : save config and data
        self.logger.info('Save configuration')
        if exc_type is not None:
            self.logger.error(
                '{}: {}'.format(exc_type, exc_value),
                exc_info=True
            )

        super(OrdersManager, self).__exit__(exc_type, exc_value, exc_tb)

    def __iter__(self):
        """ Iterate until server stop. """
        self.logger.info('Starting to wait orders')

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
            # try:
            #    self.check_available_volume(order)

            # except InsufficientFundsError:
            #    self.logger.error('Volume not available', exc_info=True)
            #    # Save order
            #    try:
            #        ief_orders = OrderDict()
            #        ief_orders._load('./strategies/', 'IFE_orders', ext='.dat')

            #    except FileNotFoundError:

            #        pass

            #    ief_orders.append(order)
            #    ief_orders._save('./strategies/', 'IFE_orders', ext='.dat')
            #    self.logger.error('Save {} in IEF_orders.dat'.format(order))
            #    # Sent info at Strategy Bot that the order wasn't executed
            #    self.conn_tbm.send(('ife', order.id),)

            #    return None

            return order

        elif self.orders:
            id_order = self.orders.get_first()

            return self.orders.pop(id_order)

        return None

    def loop(self):
        """ Run a loop until TradingBotServer closed. """
        self.logger.info('start loop method')
        for order in self:
            if order is None:
                time.sleep(0.01)

                continue

            elif order.status is None:
                self.orders.append(order)
                self.logger.debug('execute {}'.format(order))
                order.execute()

            elif order.status == 'open' or order.status == 'canceled':
                self.orders.append(order)
                order.update()

            elif order.status == 'canceled':
                self.orders.append(order)
                # TODO: check vol, replace order
                self.logger.debug('replace {}'.format(order))
                order.replace('best')

            elif order.status == 'closed':
                order.get_result_exec()
                update_hist_orders(order)
                self.conn_tbm.send(('order', order.id),)
                self.logger.debug('remove {}'.format(order))
                if not self.orders:
                    # Update fees and balance
                    self.get_fees()
                    self.get_balance()

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
        self.logger.debug('fees are loaded')

        self.conn_tbm.send(('fees', self.fees),)
        self.logger.debug('fees are sent to TBM')

    def get_balance(self):
        """ Load current balance. """
        self.balance = self.K.query_private('Balance')
        self.call_counter('Balance')
        self.logger.debug('balance is loaded')

        self.conn_tbm.send(('balance', self.balance),)
        self.logger.debug('sent balance to TBM')

    def check_available_volume(self, order, tol=0.01):
        """ Check is volume to trade is available.

        Parameters
        ----------
        order : _BasisOrder object or subclasses
            The order to executed.
        tol : float, optional
            A marge of available volume to trade. Deafult is 1% above the
            volume needed.

        """
        if order.type.lower() == 'sell':
            balance = float(self.balance[order.pair[:4]])
            volume = order.volume

        else:
            balance = float(self.balance[order.pair[4:]])
            volume = order.volume * order.price

        leverage = order.input.get('leverage')
        if leverage is not None and leverage > 1:

            return None

        elif balance < volume * (1. + tol):

            raise InsufficientFundsError(order, balance)

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
    from trading_bot.tools.io import load_config_params

    # Load logging configuration
    log_config = load_config_params('./trading_bot/logging.ini')
    logging.config.dictConfig(log_config)

    # Load general configuration
    gen_config = load_config_params('./general_config.yaml')
    path_log = gen_config['path']['log_file']

    om = OrdersManager()
    with om('kraken', path_log):
        om.logger.info('start loop method')
        # TODO : get last order
        last_order = 0
        for order in om:
            if order is None:
                # DO SOMETHING ELSE (e.g. display results_manager)
                txt = time.strftime('%y-%m-%d %H:%M:%S') + ' | Last order was '
                txt += str_time(int(time.time() - last_order)) + ' ago'
                print(txt, end='\r')
                time.sleep(0.01)

                continue

            elif order.status is None:
                om.logger.debug('execute {}'.format(order))
                order.execute()
                last_order = time.time()
                om.orders.append(order)

            elif order.status == 'open' or order.status == 'canceled':
                order.update()
                om.orders.append(order)

            elif order.status == 'canceled':
                # TODO: check vol, replace order
                om.logger.debug('replace {}'.format(order))
                order.replace('best')
                om.orders.append(order)

            elif order.status == 'closed':
                order.get_result_exec()
                update_hist_orders(order)
                # TODO : update results_manager
                om.conn_tbm.send(('order', order.id),)
                om.logger.debug('remove {}'.format(order))
                if not om.orders:
                    # Update fees and balance
                    om.get_fees()
                    om.get_balance()

            else:

                raise OrderError(order, 'unknown state')

        om.logger.info('OrdersManager stopped.')
