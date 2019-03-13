#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import time

# Import external packages
import fynance as fy

# Import local packages
from .data_requests import DataRequests, data_base_requests

__all__ = ['StrategyManager']

"""
TODO list:
    - Create a class strategy manager (or a bot object).
    - Init parameters: underlying, strategy function, step time, value allow
    to this strategy.
    - Methods: ... ?
"""


class StrategyManager:
    """ Main object to load data, compute signals and execute orders.

    Methods
    -------
    get_data : TODO
    get_signal : TODO
    set_order : TODO

    tofinish: - Init;
              - Call;
              - Isovol;

    Attributes
    ----------
    frequency : int
        Number of seconds between two samples.
    underlying : str or list ?
        Name of the underlying or list of data needed.
    strat_name : function ? str ? callable ?
        strat_name function.

    """
    def __init__(self, frequency, underlying, strat_name, STOP=None):
        """
        Parameters
        ----------
        frequency : int
            Number of seconds between two samples.
        underlying : str or list ?
            Name of the underlying or list of data needed.
        strat_name : function ? str ? callable ?
            strat_name function.
        STOP : int, optional
            Number of iteration before stoping, if `None` iteration will stop
            every 24 hours. Default is None.

        """
        self.frequency = frequency
        self.underlying = underlying
        self.strat_name = strat_name
        if STOP is None:
            self.STOP = 86400 // frequency
        else:
            self.STOP = STOP

    def __call__(self, *args, **kwargs):
        """ Set parameters of strategy.

        """
        self.args = args
        self.kwargs = kwargs

        return self

    def __iter__(self):
        """ Initialize iterative method. """
        self.t = 0
        self.TS = int(time.time())
        self.next = self.TS + self.frequency
        return self

    def __next__(self):
        """ Iterative method """
        t = self.t
        if t >= self.STOP:
            raise StopIteration
        self.TS = int(time.time())
        # Sleep until ready
        if self.next > self.TS:
            time.sleep(self.next - self.TS)
        # Update data
        # TODO : get_data
        # Compute signal
        # TODO : get_signal
        # self.compute_signal()
        # Execute order
        # TODO : set_order
        self.next += self.frequency
        self.t += 1
        return t

    def set_data(self, data):
        """ """
        # TODO : Set data, something like that
        self.data.append(data)
        return self

    def get_signal(self):
        """
        """
        # TODO
        return signal

    def set_order(self):
        """
        """
        # TODO
        return order

    def compute_signal(self):
        """ Compute signal strategy.

        """
        pass

    def _isovol(self):
        """ Iso-volatility method

        """
        fy.iso_vol()
        pass


class DataManager:
    """ Description.

    """
    def __init__(self, assets, ohlcv, frequency=60, path='data_base/',
                 request=False, url=''):
        """ Initialize parameters """
        self.assets = assets
        self.ohlcv = ohlcv
        self.freq = frequency
        self.request = request
        self.url = url

    def get_data(self, *args, **kwargs):
        """ Get data """
        if self.request:
            return DataRequests(self.url).get_data(*args, **kwargs)
        else:
            return data_base_requests(
                self.assets, self.ohlcv, self.freq, *args, **kwargs
            )
