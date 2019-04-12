#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import time
import importlib

# Import external packages

# Import local packages
from strategy_manager.tools.time_tools import now
from strategy_manager import DataBaseManager, DataExchangeManager

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
        strat = importlib.import_module(
            'strategy_manager.strategies.' + script_name + '.strategy'
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
        self.TS = now(self.frequency)
        self.next = self.TS + self.frequency

        return self

    def __next__(self):
        """ Iterative method. """
        if self.t >= self.STOP:
            raise StopIteration

        self.TS = int(time.time())

        # Sleep until ready
        if self.next > self.TS:
            time.sleep(self.next - self.TS)

        self.next += self.frequency
        self.t += 1

        # TODO : Debug/find solution to request data correctly.
        #        Need to choose between request a database, server,
        #        exchange API or other.
        data = self.DM.get_data(*self.args_data, **self.kwargs_data)

        return self.get_order_params(data)

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

    def set_data_manager(self, **kwargs):
        """ Set `DataManager` object.

        Parameters
        ----------
        kwargs : dict
            Cf `DataManager` constructor.

        """

        if 'args' in kwargs.keys():
            self.args_data = kwargs.pop('args')
        else:
            self.args_data = ()

        if 'kwargs' in kwargs.keys():
            self.kwargs_data = kwargs.pop('kwargs')
        else:
            self.kwargs_data = {}

        request_from = kwargs.pop('source_data').lower()

        if request_from == 'exchange':
            self.DM = DataExchangeManager(**kwargs)
        elif request_from == 'database':
            self.DM = DataBaseManager(**kwargs)
        else:
            raise ValueError('request_data must be exchange or database.'
                             'Not {}'.format(request_from))

        return self
