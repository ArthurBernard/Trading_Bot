#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-28 15:41:40
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-15 13:45:43

"""
Trading_Bot : A Python package to build autonomous trading bot
==============================================================

Documentation is available at [comming soon]

Contents
--------
Trading_Bot is a Python project that provides tools to build your custom
autonomous trading bot adapted to your algorithmic financial strategies.

Modules
-------
bot_manager      --- Set the bot server and run order and strategy clients
data_requests    --- Request data needed for strategy computations
order_manager    --- Set the order client and execute orders
result_manager   --- Display results of strategies and portfolio
strategy_manager --- Set a strategy client and send orders to execute

Utility tools
-------------
API_bfx     --- Bitfinex client API
API_kraken  --- Kraken client API
_client     --- Base client to connect to server
_order      --- Object representing order to execute.
_exceptions --- Trading_Bot exceptions
_server     --- Base server to run several bots
tests       --- Run trading_bot unittests
tools       --- Time, setting, and configuration tools

"""

# Built-in packages

# Third party packages

# Local packages
from .API_bfx import *
from .API_kraken import *
from .bot_manager import *
from .call_counters import *
from .data_requests import *
from .orders_manager import *
from .results_manager import *
from .strategy_manager import *
from .tools import *


__all__ = API_bfx.__all__
__all__ += API_kraken.__all__
__all__ += bot_manager.__all__
__all__ += call_counters.__all__
__all__ += data_requests.__all__
__all__ += orders_manager.__all__
__all__ += results_manager.__all__
__all__ += strategy_manager.__all__
__all__ += tools.__all__
