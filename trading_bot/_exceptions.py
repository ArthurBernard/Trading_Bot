#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-04 16:04:55
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-09 11:04:28

""" Define some various richly-typed exceptions. """

# Built-in packages

# Third party packages

# Local packages


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
