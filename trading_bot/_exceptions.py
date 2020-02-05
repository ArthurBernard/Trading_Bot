#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-04 16:04:55
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-05 16:00:32

""" Define some various richly-typed exceptions. """

# Built-in packages

# Third party packages

# Local packages


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
