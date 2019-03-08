#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
from pickle import Pickler, Unpickler
import time

# Import external packages
import fynance as fy

# Import local packages

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
    timestep : int
        Number of seconds between two samples.
    underlying : str or list ?
        Name of the underlying or list of data needed.
    strategy : function ? str ? callable ?
        Strategy function.

    """
    def __init__(self, timestep, underlying, strategy, volume,
        time_exec=0, data_request_on_the_fly=True, STOP=None):
        """
        Parameters
        ----------
        timestep : int
            Number of seconds between two samples.
        underlying : str or list ?
            Name of the underlying or list of data needed.
        strategy : function ? str ? callable ?
            Strategy function.
        volume : float
            Quantity to trade.
        iso_vol : bool, optional
            If true apply iso-volatility filter to signal, default is True.
        time_exec : int or str, optional
            TODO : description
        data_request_on_the_fly : bool, optional
            Request data on the fly if true, else request a data base. 
            Defaut is True. 
        STOP : int, optional
            Number of iteration before stoping, if `None` iteration will stop 
            every 24 hours. Default is None.

        """
        self.timestep = timestep
        self.underlying = underlying
        self.strategy = strategy
        self.volume = volume
        self.time_exec = time_exec
        self.data_request_on_the_fly = data_request_on_the_fly
        if STOP is None:
            self.STOP = 86400 // timestep
        else:
            self.STOP = STOP

    def __call__(self, *args, iso_vol=True, **kwargs):
        """ Set parameters of strategy.
         
        """
        self.args = args
        self.kwargs = kwargs

        return self

    def __iter__(self):
        """ Initialize iterative method. """
        self.t = 0
        if self.time_exec == 'now':
            self.TS = time.time()
        else:
            self.TS = int(time.time()) // self.timestep * self.timestep 
            self.TS += self.time_exec
        self.next = self.TS + self.timestep
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
        self.next += self.timestep
        self.t += 1
        return t

    def set_data_loader(self, timestep, underlying, sample_size=None, since=None, 
        last=None):
        """ Instanciate data loader object """
        if data_request_on_the_fly:
            # TODO : set parameters
            self.data_loader = DataRequests()
        else:
            # TODO : set parameters
            self.data_loader = DataLoader()
        return self

    def get_data(self):
        """ Get Data method.

        """
        # TODO : request data
        return self.data_loader.get_data()

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
        #self.signal = self.strategy(*self.args, **self.kwargs)
        #if self.iso_vol:
        #    self._isovol()
        #pass

    def _isovol(self):
        """ Iso-volatility method
        
        """
        fy.iso_vol()
        pass