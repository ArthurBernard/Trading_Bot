#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-25 10:38:17
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-25 10:45:39

""" Objects to measure and display trading performance. """

# Built-in packages
import logging

# Third party packages

# Local packages
from trading_bot._client import _ClientTradingPerformance


class TradingPerformance(_ClientTradingPerformance):
    """ TradingPerformance object. """

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        super(TradingPerformance, self).__inti__(
            address=address,
            authkey=authkey
        )
        self.logger = logging.getLogger(__name__)
