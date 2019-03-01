#!/usr/bin/env python3
# coding: utf-8

import time

"""
Some functions to manage date, timestamp and other time format.

TODO:
    - Finish date_to_TS and TS_to_date functions
"""

def date_to_TS(date, format='%d-%m-%y %H:%M:%S'):
    """
    Parameters
    ----------
    :date: int, str, date, etc ?
        Date to convert to timestamp
    :format: str
        Format of input date.

    Return
    ------
    Timestamp of the date.
    """
    if isinstance(date, int):
        return date
    elif isinstance(date, str):
        # TODO
        pass
    else:
        print('Date format not allowed')
        raise Error

def TS_to_date(TS, format='%d-%m-%y %H:%M:%S'):
    """
    Parameters
    ----------
    :TS: int, str, date, etc ?
        Timestamp to convert to date.
    :format: str
        Format of output date.

    Return
    ------
    Date of the timestamp.
    """
    if isinstance(date, int):
        # TODO
        pass
    elif isinstance(date, str):
        return TS
    else:
        print('Timestamp format not recognized')
        raise Error