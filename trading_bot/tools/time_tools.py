#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-03-23 11:36:05
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-05 17:41:40

""" Tools to manage date and time. """

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


def str_time(t):
    """ Return the time such as %H:%M:%S.

    Parameters
    ----------
    t : int
        Time in seconds.

    Returns
    -------
    str
        Time in hours, minutes and seconds.

    """
    txt = ''
    s, t = t % 60, t // 60
    if s < 10:
        s = '0' + str(s)

    m, h = t % 60, t // 60
    if m < 10:
        m = '0' + str(m)

    if h > 24:
        h, d = h % 24, h // 24

        txt += str(d) + ' days '

    if h < 10:
            h = '0' + str(h)

    return txt + '{}:{}:{}'.format(h, m, s)


def date_to_TS(date, format='%y-%m-%d %H:%M:%S'):
    """ Convert date to timestamp.

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

        return time.mktime(time.strptime(date, format))

    else:
        print('Date format not allowed')

        raise ValueError('Unknow format', type(date))


def TS_to_date(TS, format='%y-%m-%d %H:%M:%S'):
    """ Convert timestamp to date.

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

        return time.strftime(format, time.localtime(TS))

    elif isinstance(TS, str):

        return TS

    else:
        print('Timestamp format not recognized')

        raise ValueError('Unknow format', type(TS))
