#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-03-01 19:14:37
# @Last modified by: ArthurBernard
# @Last modified time: 2019-09-04 08:30:44

""" Object to load data. Not yet working. """

# Built-in packages

# Third party packages
from dccd.date_time import date_to_TS

# Local packages

__all__ = ['DataLoader']

# TODO list:
# - To create method: Load data;
# - To create method: Get ready data;


class DataLoader:
    """ Read, sort and clean data and get ready to send to strategy manager.

    Parameters
    ----------
    timestep : int
        Number of seconds between two samples.
    underlying : str or list ?
        Name of the underlying or list of data needed.
    sample_size : int (default is None)
        Size of data sample needed.
    since : str data (%d-%m-%y %H:%M:%S) or int timestamp.
        Date of first observation of data sample (default is the first
        observation of the day). This parameter is ignored if sample_size
        is specified.
    last : str data (%d-%m-%y %H:%M:%S) or int timestamp.
        Date of last observation of data sample (default is now).

    Attributes
    ----------
    timestep : int
        Number of seconds between two samples.
    underlying : str or list ?
        Name of the underlying or list of data needed.
    since : str date (%d-%m-%y %H:%M:%S) or int timestamp.
        Date of first observation of data sample (default is the first
        observation of the day). This parameter is ignored if sample_size
        is specified.
    last : data (%d-%m-%y %H:%M:%S) or timestamp.
        Date of last observation of data sample (default is now).

    """

    def __init__(self, timestep, underlying, sample_size=None, since=None,
                 last=None):
        """ Initialize. """
        self.timestep = timestep
        self.underlying = underlying
        self.last = date_to_TS(last)

        if sample_size is not None:
            self.since = self.last - timestep * sample_size

        elif since is not None:
            self.since = date_to_TS(since)

        else:
            sample_size = (last % 86400) * 86400
            self.since = self.last - timestep * sample_size
