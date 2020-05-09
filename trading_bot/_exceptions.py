#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-04 16:04:55
# @Last modified by: ArthurBernard
# @Last modified time: 2020-05-09 16:43:10

""" Define some various richly-typed exceptions. """

# Built-in packages

# Third party packages

# Local packages

# =========================================================================== #
#                         Errors with order objects                           #
# =========================================================================== #


class OrderError(Exception):
    """ Order exception. """

    def __init__(self, order, msg=None, msg_prefix=''):
        """ Initialize the order exception. """
        if msg is not None:
            msg = '[Order ID {}] - '.format(order.id) + msg

        else:
            msg = str(order)

        super(OrderError, self).__init__(msg_prefix + msg)


class OrderStatusError(OrderError):
    """ Raise an exception when an action isn't allowed by current status. """

    def __init__(self, order, action):
        """ Initialize the OrderStatusError exception. """
        msg = 'can not {} order with status {}'.format(action, order.status)
        super(OrderStatusError, self).__init__(order, msg)


class MissingOrderError(Exception):
    """ Order is missing. """

    def __init__(self, id_order, msg_prefix=None, params=None):
        """ Initialize the missing order exception. """
        msg = 'the order {} is missing'.format(id_order)

        if params is not None:
            msg += ', parameters was {}'.format(params)

        if msg_prefix is not None:
            msg = '{}: {}'.format(msg_prefix, msg)

        super(MissingOrderError, self).__init__(msg)


class InsufficientFundsError(OrderError):
    """ Balance volume is insufficient. """

    def __init__(self, order, available_volume=None):
        """ Initialize the insufficient funds error. """
        msg = "Insufficient funds for "
        msg += f"{order.type} {order.volume} {order.pair[1:4]}"
        if order.type == 'buy':
            ccy = order.pair[-3:]
            cost = order.volume / order.price
            msg += f" at {cost} {ccy}"

        else:
            ccy = order.pair[:3]

        if available_volume is not None:
            msg += f" only {available_volume} {ccy} is available."

        super(InsufficientFundsError, self).__init__(order, msg)


class InsufficientFunds(Exception):
    """ Balance volume is insufficient. """

    def __init__(self, ccy, type, vol, avail_vol, price, ccy_2):
        """ Initialize the insufficient funds error. """
        msg = "Insufficient funds for "
        msg += f"{type} {vol}"
        if type == 'buy':
            cost = vol * price
            msg += f" {ccy_2} at {cost} {ccy}"

        else:
            msg += f" {ccy}"

        msg += f" only {avail_vol} {ccy} is available."

        super(InsufficientFunds, self).__init__(msg)


# =========================================================================== #
#                       Errors with connection objects                        #
# =========================================================================== #


class ConnError(Exception):
    """ Connection exception. """

    pass


class ConnRefused(ConnError):
    """ Connection refused exception. """

    def __init__(self, _id, msg=None, msg_prefix=None):
        txt = '{}'.format(_id)
        if msg is not None:
            txt += ' ' + msg

        if msg_prefix is not None:
            txt = msg_prefix + ', ' + txt

        super(ConnRefused, self).__init__(txt)
