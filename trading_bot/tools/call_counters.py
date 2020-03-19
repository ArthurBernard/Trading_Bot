#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-15 12:34:18
# @Last modified by: ArthurBernard
# @Last modified time: 2020-03-19 08:09:12

""" Some call counter objects. """

# Built-in packages
import time
import logging

# Third party packages

# Local packages


__all__ = ['KrakenCallCounter']


class _CallCounter(object):
    """ Basis of call counter object.

    At each request a counter is increased by a number follwoing request
    method. The counter is reduced by one every couple of seconds.

    Attributes
    ----------
    t : int
        Timestamp of the last update of the counter.
    counter : int
        Number of the current counter call.
    call_rate_limit : int
        Number max of counter call before user's account is banned.

    Methods
    -------
    __init__
    __call__

    """

    counter = 0

    def __init__(self, time_down, call_rate_limit):
        """ Initialize counter object.

        Parameters
        ----------
        time_down: int
            Number of seconds to wait to decrease of one the counter.
        call_rate_limit: int
            Max call rate counter.

        """
        self.logger = logging.getLogger(__name__)
        self.time_down = time_down
        self.call_rate_limit = call_rate_limit
        self.t = int(time.time())

    def __call__(self, pt):
        """ Increase the counter and wait if necessary.

        The counter is increased by `pt` each time it is called. And if the
        counter will exceed the `call_rate_limit` then the object wait a couple
        of seconds to prevent a ban of the user account.

        Parameters
        ----------
        pt : int
            Number to increase the call rate counter.

        """
        # increase counter
        self.counter += pt
        t = int(time.time())
        # down counter
        self.counter -= (t - self.t) // self.time_down
        self.counter = max(self.counter, 0)
        self.t = t
        self.logger.debug('counter={}'.format(self.counter))
        # check if call_rate_limit is exceeded
        if self.counter >= self.call_rate_limit - 1:  # substract 1 to prevent
            self.logger.info('counter exceeds {}'.format(self.call_rate_limit))
            time.sleep(self.counter - self.call_rate_limit + 1)


class KrakenCallCounter(_CallCounter):
    """ CallCounter object dedicated for Kraken Client API.

    At each request a counter is increased by a number follwoing request
    method. The counter is reduced by one every couple of seconds, depending on
    the status of verification of the user's account.

    Attributes
    ----------
    t : int
        Timestamp of the last update of the counter.
    counter : int
        Number of the current counter call.
    call_rate_limit : int
        Number max of counter call before user's account is banned.

    Methods
    -------
    __init__
    __call__

    """

    _handler_method = {
        'AddOrder': 0,
        'CancelOrder': 0,
        'Balance': 1,
        'TradeBalance': 1,
        'OpenOrders': 1,
        'ClosedOrders': 1,
        'QueryOrders': 1,
        'QueryTrades': 1,
        'OpenPositions': 1,
        'TradeVolume': 1,
        'AddExport': 1,
        'ExportStatus': 1,
        'RetrieveExport': 1,
        'RemoveExport': 1,
        'TradesHistory': 2,
        'Ledgers': 2,
        'QueryLedgers': 2,  # not sure
    }

    def __init__(self, status_verified_user):
        """ Initialize the Kraken's counter object.

        Parameters
        ----------
        status_verified_user : {'starter', 'intermediate', 'pro'}
            Status of verification of the Kraken user account.

        """
        if status_verified_user.lower() == 'starter':
            time_down, call_rate_limit = 3, 15

        elif status_verified_user.lower() == 'intermediate':
            time_down, call_rate_limit = 2, 20

        elif status_verified_user.lower() == 'pro':
            time_down, call_rate_limit = 1, 20

        else:
            raise ValueError('Invalid status_verified_user {}'.format(
                status_verified_user
            ))

        super(KrakenCallCounter, self).__init__(time_down, call_rate_limit)
        self.logger = logging.getLogger(__name__)

    def __call__(self, method):
        """ Increase the Kraken counter and wait if necessary.

        The counter is increased by 0, 1 or 2 depending on the request method.
        If the counter will exceed the `call_rate_limit` attribute, then the
        object waits a couple of seconds to prevent a ban of the user account.

        Parameters
        ----------
        method : str
            Name of a private request to the Kraken Client API.

        """
        pt = self._handler_method.get(method)
        if pt is None:

            raise ValueError('Unknown method {}'.format(method))

        super(KrakenCallCounter, self).__call__(pt)
