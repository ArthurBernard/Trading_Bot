#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import time
import importlib

# Import external packages

# Import local packages

__all__ = ['StrategyManager']


class StrategyManager:
    """ Main object to load data, compute signals and execute orders.

    Methods
    -------
    get_signal(data)
        Computes and returns signal' strategy.
    __call__(*args, **kwargs)
        Set parameters to pass into function' strategy.
    set_iso_vol(series, target_vol=0.2, leverage=1., period=252, half_life=11)
        Computes and returns iso-volatility coefficient.

    Attributes
    ----------
    frequency : int
        Number of seconds between two samples.
    underlying : str
        Name of the underlying or list of data needed.
    _get_signal : function
        Function to get signal' strategy.

    """

    def __init__(self, frequency, underlying, script_name, STOP=None,
                 iso_volatility=True):
        """
        Parameters
        ----------
        frequency : int
            Number of seconds between two samples.
        underlying : str
            Name of the underlying or list of data needed.
        script_name : str
            Name of script to load function strategy (named `get_signal`).
        STOP : int, optional
            Number of iteration before stoping, if `None` iteration will
            stop every 24 hours. Default is `None`.
        iso_volatility : bool, optional
            If true apply a coefficient of money management computed from
            underlying volatility. Default is `True`.

        """
        # strat = __import__(script_name, fromlist=['strategies'])
        strat = importlib.import_module(
            'strategy_manager.strategies.' + script_name
        )
        self._get_order_params = strat.get_order_params
        self.frequency = frequency
        self.underlying = underlying
        if STOP is None:
            self.STOP = 86400 // frequency
        else:
            self.STOP = STOP
        self.iso_vol = iso_volatility

    def __call__(self, *args, **kwargs):
        """ Set parameters of strategy.

        Parameters
        ----------
        args : tuple, optional
            Any arguments to pass into function' strategy.
        kwargs : dict, optionl
            Any keyword arguments to pass into function' strategy.

        Returns
        -------
        StrategyManager
            Object to manage strategy computations.

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
        """ Iterative method. """
        t = self.t
        if t >= self.STOP:
            raise StopIteration
        self.TS = int(time.time())
        # Sleep until ready
        if self.next > self.TS:
            time.sleep(self.next - self.TS)
        self.next += self.frequency
        self.t += 1
        return t

    def get_order_params(self, data):
        """ Function to compute signal, price and volume.

        Parameters
        ----------
        data : pandas.DataFrame
            Data to compute signal' strategy.

        Returns
        -------
        signal, price, volume : foat
            Signal, price and volume strategy.

        """
        return self._get_order_params(data, *self.args, **self.kwargs)
