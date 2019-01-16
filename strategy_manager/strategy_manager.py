#!/usr/bin/env python
# -*- coding: utf-8 -*-

from pickle import Pickler, Unpickler
import time

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
    def __init__(self, timestep, underlying, strategy):
        """
        Parameters
        ----------
        :timestep: int
            Number of seconds between two samples.
        :underlying: str or list ?
            Name of the underlying or list of data needed.
        :strategy: function ? str ? callable ?
            Strategy function.
        """
        self.timestep = timestep
        self.underlying = underlying
        self.strategy = strategy

    def __call__(self, *args, **kwargs):
        """ 
        Call strategy signal.
        Apply iso-volatility ? 
        """
    	self.signal = self.strategy(*args, **kwargs)
    	pass

    def _isovol(self):
    	""" 
    	Iso-volatility method
    	"""
    	pass