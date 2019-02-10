#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Import built-in packages
from pickle import Pickler, Unpickler
import time

# Import external packages
import fynance as fy

# Import local packages

"""
TODO list:
    - Create a class strategy manager (or a bot object).
    - Init parameters: underlying, strategy function, step time, value allow 
    to this strategy.
    - Methods: ... ?
"""

class StrategyManager:
    """
    Description..
    
    Methods
    -------
    todo: - ?

    tofinish: - Init;
              - Call;
              - Isovol;

    Attributes
    ----------
    :timestep: int
        Number of seconds between two samples.
    :underlying: str or list ?
        Name of the underlying or list of data needed.
    :strategy: function ? str ? callable ?
        Strategy function.
    """
    def __init__(self, timestep, underlying, strategy, iso_vol=True, time_exec=0):
        """
        Parameters
        ----------
        :timestep: int
            Number of seconds between two samples.
        :underlying: str or list ?
            Name of the underlying or list of data needed.
        :strategy: function ? str ? callable ?
            Strategy function.
        :iso_vol: bool
            If true apply iso-volatility filter to signal.
        :time_exec: int or str

        """
        self.timestep = timestep
        self.underlying = underlying
        self.strategy = strategy
        self.iso_vol = iso_vol
        self.time_exec

    def __call__(self, *args, iso_vol=True, **kwargs):
        """ Set parameters of strategy.
         
        """
        self.args = args
        self.kwargs = kwargs

        return self

    def __iter__(self):
        """ Initialize iterative method. """
        self.t = 0
        if time_exec == 'now':
            self.TS = time.time()
        else:
            self.TS = time.time() // timestep * timestep + time_exec
        self.next = self.TS + 1
        return self

    def __next__(self):
        """ Iterative method """
        t = self.t
        if t >= self.STOP:
            raise StopIteration
        self.TS = time.time()
        # Sleep until ready
        if self.next > self.TS:
            time.wait(self.next - self.TS)
        # Update data
        # TODO
        # Compute signal
        self.compute_signal()
        # Execute order
        # TODO
        self.next += timestep
        self.t += 1
        return self

    def compute_signal(self):
        """ Compute signal strategy.

        """
        self.signal = self.strategy(*self.args, **self.kwargs)
        if self.iso_vol:
            self._isovol()
        pass

    def _isovol(self):
        """ 
        Iso-volatility method
        """
        fy.iso_vol()
        pass