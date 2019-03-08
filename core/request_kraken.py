#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import json 
import time
import sys

# Import external packages
import requests

__all__ = ['ReqKraken']

class ReqKraken:
    """ Class to request data from Kraken exchange with REST public API.
    
    Methods
    -------
    - get_data : return data in list or dict.

    Attributes
    ----------

    
    """
    def __init__(self, request, stop_step=1, last_ts=0):
        """ Set kind of request, time step in second between two requests, 
        and if necessary from when (timestamp).

        Parameters
        ----------
        request : str
            Kind of REST public API request.
        stop_step : int, optional
            Max number of request, default is 1.
        last_ts : int, optional
            Timestamp of last observation if exist else 0. Default is 0.
        
        """
        self.url = "https://api.kraken.com/0/public/"
        self.request = request
        self.t = 0
        self.stop_step = stop_step
        self.last_ts = last_ts
        
    def get_data(self, **params):
        """ Request data to public REST API from Kraken.

        Parameters
        ----------
        params : dict 
            Cf Kraken API documentation [1]_ .

        Returns
        -------
        data : dict of dict
            Requested data.

        References
        ----------
        .. [1] https://www.kraken.com/help/api

        Examples
        --------
        >>> req = ReqKraken('OHLC', stop_step=1)
        >>> req.get_data(pair='ETHUSD')['error']
        []

        """
        # Set timestamp of the observation
        self.last_ts = int(time.time())
        # Requests data
        ans = requests.get(
            self.url + self.request, 
            params
        )
        # Returns result
        try:
            return json.loads(ans.text)
        except Exception as error:
            txt = '\nUNKNOWN ERROR\n'
            txt += 'In {} script, at {}, the following error occurs: {}\n'.format(
                sys.argv[0], 
                time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time())), 
                str(error)
            )
            with open('errors/{}.log'.format(sys.argv[0]), 'a') as f:
                f.write(txt)
            time.sleep(1)
            return self.get_data(**params)
    
    def __iter__(self):
        return self
    
    def __next__(self):
        # Stop iteration 
        if self.t >= self.stop_step:
            raise StopIteration
        # Sleep
        elif self.last_ts + self.time_step > time.time():
            time.sleep(self.last_ts + self.time_step - time.time())
        # Run 
        self.t += 1
        return self.get_data(**self.params)
    
    def __call__(self, time_step=2, **params):
        # Set timestep and request's parameters
        self.time_step = time_step
        self.params = params
        return self

if __name__ == '__main__':
    import doctest
    doctest.testmod()