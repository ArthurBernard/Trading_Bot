#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import json 
import time
import sys
from pickle import Unpickler

# Import external packages
import requests
import pandas as pd

__all__ = ['DataRequests']

class DataRequests:
    """ Class to request data from an exchange with REST public API.
    
    Methods
    -------
    - get_data : return data in list or dict.

    Attributes
    ----------

    
    """
    def __init__(self, request, public_api_url, stop_step=1, last_ts=0):
        """ Set kind of request, time step in second between two requests, 
        and if necessary from when (timestamp).

        Parameters
        ----------
        request : str
            Kind of REST public API request.
        public_api_url : str
            Url of an exchange public API REST.
        stop_step : int, optional
            Max number of request, default is 1.
        last_ts : int, optional
            Timestamp of last observation if exist else 0. Default is 0.
        
        """
        self.url = public_api_url
        self.request = request
        self.t = 0
        self.stop_step = stop_step
        self.last_ts = last_ts
        
    def get_data(self, *args, **kwargs):
        """ Request data to public REST API from an Exchange.

        Parameters
        ----------
        args : tuple
            Each element of the tuple is added to the url separated with `/`.
        kwargs : dict 
            Each key words is append to parameters at the requests. 
            Cf documentation of the exchange API for more details.

        Returns
        -------
        data : dict of dict
            Requested data.

        Examples
        --------
        >>> req = ReqKraken('OHLC', "https://api.kraken.com/0/public/", stop_step=1)
        >>> req.get_data(pair='ETHUSD')['error']
        []

        """
        # Set timestamp of the observation
        self.last_ts = int(time.time())
        url = self.url + self.request
        for arg in args:
            url += arg + '/'
        # Requests data
        ans = requests.get(url, kwargs)
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
            return self.get_data(**kwargs)
    
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
        return self.get_data(**self.kwargs)
    
    def __call__(self, time_step=2, **kwargs):
        # Set timestep and request's parameters
        self.time_step = time_step
        self.kwargs = kwargs
        return self


def data_base_requests(assets, ohlcv, frequency=60, start=None, end=None):
    """ Function to request in the data base one or several ohlcv data assets
    from a specified date to an other specified data and at a specified 
    frequency. 

    Parameters
    ----------
    assets : str or list of str
        Id(s) of the asset(s) to requests.
    ohlcv : str or list of str
        Kind of price data to requests, following are available 'o' to open, 
        'h' to high, 'l' to low, 'c' to close and 'v' to volume.
    frequency : int, optional
        Number of second between two data observations (> 60).
    start : int, optional
        Timestamp to start data request, default start request at last data 
        availabe.
    end : int, optional
        Timestamp to end data request, default end request at last data 
        available.

    Returns
    -------
    data : pd.DataFrame
        A data frame with the data requested.

    Examples
    --------

    See Also
    --------

    """
    if end is None:
        end = int(time.time()) // 60 * 60
    data = pd.DataFrame()
    for asset in assets:
        if start is None:
            date = time.strftime('%y-%m-%d', time.gmtime(time.time()))
            path = '../data_base/' + asset + '/' + date
            df = _data_base_requests(path, slice(None), ohlcv).iloc[-1, :]
        else:
            df = pd.DataFrame()
            for row_slice in _set_row_slice(start, end, frequency):
                date = time.strftime('%y-%m-%d', time.gmtime(row_slice[0]))
                path = '../data_base/' + asset + '/' + date
                subdf = _data_base_requests(path, row_slice, ohlcv)
                df.append(subdf)
        data = data.join(df, rsuffix=asset)

    return data


def _data_base_requests(path, row_slice, col_slice):
    # Load data base
    with open(path, 'rb') as f:
        df = Unpickler(f).load()
    # Return specified data
    return df.loc[row_slice, col_slice]


def _set_row_slice(start, end, frequency):
    i = 0
    row_slices = []
    end += frequency
    while i < (end - start) // 86400:
        row_slice += [range(start, min(start + 86400, end), frequency)]
        start += 86400
    return row_slice


if __name__ == '__main__':
    import doctest
    doctest.testmod()