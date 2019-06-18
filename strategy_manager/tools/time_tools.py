#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-03-23 11:36:05
# @Last modified by: ArthurBernard
# @Last modified time: 2019-06-17 15:30:42

# Built-in packages
import time

# External packages

# Local packages

__all__ = ['date_to_TS', 'TS_to_date', 'now']

"""
Some functions to manage date, timestamp and other time format.

TODO:
    - Finish date_to_TS and TS_to_date functions
"""


def now(freq=60):
    """ Return timestamp of the beging period `freq`.

    Parameters
    ----------
    freq :  int, optional
        Number of second of one period, default is 60 (minutely).

    Returns
    -------
    int
        Timestamp of the current period.

    """
    return int(time.time() // freq * freq)


def date_to_TS(date, format='%d-%m-%y %H:%M:%S'):
    """ Convert date to timestamp.
    TODO : To finish !

    Parameters
    ----------
    date : int, str or date
        Date to convert to timestamp
    format : str
        Format of input date.

    Return
    ------
    int
        Timestamp of the date.

    """
    if isinstance(date, int):
        return date
    elif isinstance(date, str):
        # TODO
        pass
    else:
        print('Date format not allowed')
        raise ValueError('Unknow format', type(date))


def TS_to_date(TS, format='%d-%m-%y %H:%M:%S'):
    """ Convert timestamp to date.
    TODO : To finish !

    Parameters
    ----------
    TS : int, str or date
        Timestamp to convert to date.
    format : str
        Format of output date.

    Return
    ------
    date
        Date of the timestamp.

    """
    if isinstance(TS, int):
        # TODO
        pass
    elif isinstance(TS, str):
        return TS
    else:
        print('Timestamp format not recognized')
        raise ValueError('Unknow format', type(TS))
