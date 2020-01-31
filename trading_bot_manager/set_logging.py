#!/usr/bin/env python
# coding: utf-8

# Import built-in packages
import logging
from logging.handlers import RotatingFileHandler

# Set logger object
logger = logging.getLogger('strat_man')
# Set level of logger
logger.setLevel(logging.DEBUG)

# Set format
formatter = logging.Formatter('%(asctime)s :: %(levelname)s :: %(message)s')
# Set file handler with 1Mo
path = '/home/arthur/GitHub/Strategy_Manager/strategy_manager/activity.log'
file_handler = RotatingFileHandler(path, 'a', 1000000, 1)

file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Set terminal handler
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.DEBUG)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)
