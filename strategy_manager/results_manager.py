#!/usr/bin/env python3
# coding: utf-8

# Built-in packages
import time

# External packages

# Internal packages

__all__ = ['print_results']

"""
TODO:
    - Function to save historic (signal, prices, volume, time interval, ?)
    - Print stats about strategy
    - Profit and loss
    - Plot strategy graph vs underlying

"""


def print_results(out):
    now = time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time()))
    txt = ''
    txt += '\nAt {}: {}\n'.format(now, str(out))
    print(txt)


def set_statistic():
    # TODO : set stats, profit and loss, etc
    pass


def get_historic(path):
    try:
        # TODO : load data
        pass
    except FileNotFoundError:
        # TODO : set data file
        pass


def set_historic(path):
    pass
