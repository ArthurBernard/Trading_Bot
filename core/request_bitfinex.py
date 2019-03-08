#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import json 
import time
import sys

# Import external packages
import requests

__all__ ['ReqBitfinex']

class ReqBitfinex:
    """ Class to request data from Bitfinex exchange with REST public API.
    
    Methods
    -------
    - get_data : return data in list or dict.

    Attributes
    ----------
    
    """
    def __init__(self, request, currency_pair, stop_step=1, last_ts=0):
        """
        Parameters
        ----------
        request : str
            Kind of REST public API request.
        currency_pair : str
            Currency pair requested (ex: ETHBTC or ETHUSD).
        stop_step : int, optional
            Max number of request, default is 1.
        last_ts : int, optional
            Timestamp of last observation if exist else 0. Default is 0.
        
        """
        self.url = "https://api.bitfinex.com/v1/"
        self.request = request
        self.currency_pair = currency_pair
        self.t = 0
        self.stop_step = stop_step
        self.last_ts = last_ts
        
    def get_data(self, **params):
        """ Request data to public REST API from Bitfinex.

        Parameters
        ----------
        params : dict 
            Cf Bitfinex API documentation [1]_ .

        Returns
        -------
        data : list of dict
            Requested data.

        References
        ----------
        .. [1] https://docs.bitfinex.com/docs
        
        """
        # Set timestamp of the observation
        self.last_ts = int(time.time())
        # Requests data
        ans = requests.get(
            self.url + self.request + '/' + self.currency_pair, 
            params
        )
        # Returns result
        try:
            return json.loads(ans.text)
        # Catch ConnectionError
        except requests.exceptions.ConnectionError as error: # ConnectionError
            print('In {} script, at {}, the following error occurs {}: {}\n'.format(
                sys.argv[0], 
                time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time())), 
                str(type(error)),
                str(error)
            ))
            time.sleep(1)
            return self.get_data(**params)
        # Catch SSLError
        except requests.exceptions.SSLError as error:
            print('In {} script, at {}, the following error occurs {}: {}\n'.format(
                sys.argv[0], 
                time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time())), 
                str(type(error)),
                str(error)
            ))
            time.sleep(1)
            return self.get_data(**params)
        # Catch JSONDecodeError
        except json.decoder.JSONDecodeError as error:
            print('In {} script, at {}, the following error occurs {}: {}\n'.format(
                sys.argv[0], 
                time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time())), 
                str(type(error)),
                str(error)
            ))
            time.sleep(1)
            return self.get_data(**params)
        # Unknown error
        except Exception as error:
            txt = '\nUNKNOWN ERROR\n'
            txt += 'In {} script, at {}, the following error occurs {}: {}\n'.format(
                sys.argv[0], 
                time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time())),
                str(type(error)), 
                str(error)
            )
            with open('errors/{}.log'.format(sys.argv[0]), 'a') as f:
                f.write(txt)
            raise error
    
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