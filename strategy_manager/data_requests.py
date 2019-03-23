#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import json
import time
import sys
from pickle import Unpickler
from os import listdir

# Import external packages
import requests
import pandas as pd
import numpy as np

__all__ = [
    'DataRequests', 'data_base_requests', 'aggregate_data', 'DataManager',
    'set_dataframe',
]

"""
TODO:
   - update data base
   - save data base

"""


class DataRequests:
    """ Class to request data from an exchange with REST public API.

    Methods
    -------
    get_data(*args, **kwargs)
        Return data in list or dict.

    Attributes
    ----------
    url : str
        Url of an exchange public API REST.
    stop_step : int
        Max number of request.
    last_ts : int
        Timestamp of last observation if exist else `0`.
    t : int
        The `t` th request.


    """

    def __init__(self, public_api_url, stop_step=1, last_ts=0):
        """ Set kind of request, time step in second between two requests,
        and if necessary from when (timestamp).

        Parameters
        ----------
        public_api_url : str
            Url of an exchange public API REST.
        stop_step : int, optional
            Max number of request, default is `1`.
        last_ts : int, optional
            Timestamp of last observation if exist else `0`. Default is `0`.

        """
        self.url = public_api_url
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
        >>> req = DataRequests("https://api.kraken.com/0/public", stop_step=1)
        >>> req.get_data('OHLC', pair='ETHUSD')['error']
        []

        """
        # Set timestamp of the observation
        self.last_ts = int(time.time())
        url = self.url
        for arg in args:
            url += '/' + arg
        # Requests data
        ans = requests.get(url, kwargs)
        # Returns result
        try:
            return json.loads(ans.text)
        except Exception as error:
            txt = '\nUNKNOWN ERROR\n'
            txt += 'In {} script, at {}, '.format(
                sys.argv[0],
                time.strftime('%y-%m-%d %H:%M:%S', time.gmtime(time.time())),
            )
            txt += 'the following error occurs: {}\n'.format(
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


def data_base_requests(assets, ohlcv, frequency=60, start=None, end=None,
                       path='data_base/'):
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
    path : str
        Path to load data.

    Returns
    -------
    data : pd.DataFrame
        A data frame with the data requested.

    Examples
    --------
    >>> data_base_requests(['example', 'other_example'], 'clhv')
                c  l  h  ...  l_other_example h_other_example v_other_example
    1552155180  0  0  0  ...                0               0             150
    <BLANKLINE>
    [1 rows x 8 columns]
    >>> start, end = 1552089600, 1552155180
    >>> df = data_base_requests('example', 'c', start=start, end=end)
    >>> df.iloc[0:1, :]
                c
    1552089600  0
    >>> df.iloc[-1:,:]
                c
    1552155180  0

    See Also
    --------
    aggregate_data, DataRequests

    """
    if end is None:
        end = int(time.time()) // frequency * frequency

    if isinstance(assets, str):
        assets = [assets]

    if isinstance(ohlcv, str):
        ohlcv = [i for i in ohlcv]

    # Set data by asset
    asset = assets.pop(0)
    data = _subdata_base_requests(asset, ohlcv, frequency, start, end, path)

    for asset in assets:
        df = _subdata_base_requests(asset, ohlcv, frequency, start, end, path)

        data = data.join(df, rsuffix='_' + asset)

    return data


def _subdata_base_requests(asset, ohlcv, frequency, start, end, path):
    if start is None:
        path_file = _get_last_file(path + asset)
        df = _data_base_requests(path_file, slice(None), ohlcv)
        if frequency > 60:
            df = aggregate_data(df, frequency // 60)
        return df.iloc[-1:, :]
    else:
        row_slices = _set_row_slice(start, end, frequency)
        row_slice = row_slices.pop(0)
        date = time.strftime('%y-%m-%d', time.gmtime(row_slice[0]))
        path_file = path + asset + '/' + date + '.dat'
        df = _data_base_requests(path_file, row_slice, ohlcv)
        for row_slice in row_slices:
            date = time.strftime('%y-%m-%d', time.gmtime(row_slice[0]))
            path_file = path + asset + '/' + date + '.dat'
            subdf = _data_base_requests(path_file, row_slice, ohlcv)
            df.append(subdf)
        if frequency > 60:
            df = aggregate_data(df, frequency // 60)
        return df


def _data_base_requests(path, row_slice, col_slice):
    # Load data base
    with open(path, 'rb') as f:
        df = Unpickler(f).load()
    # Return specified data
    return df.loc[row_slice, col_slice]


def _set_row_slice(start, end, frequency):
    i = 0
    row_slice = []
    STOP = (end - start) // 86400
    while i <= STOP:
        last = (start // 86400 + 1) * 86400
        row_slice += [range(start, min(last, end), frequency)]
        start += last
        i += 1
    return row_slice


def _get_last_file(path):
    files = listdir(path)
    return path + '/' + max(files)


def aggregate_data(df, win):
    """ Aggregate OHLCV data frame.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    win : int
        Number of periods to aggregate.

    Returns
    -------
    df : pandas.DataFrame
        Aggregated data.

    See Also
    --------
    data_base_requests

    """
    for c in df.columns:
        if c == 'h':
            df.loc[::-1, c] = df.loc[::-1, c].rolling(win, min_periods=0).max()
        elif c == 'l':
            df.loc[::-1, c] = df.loc[::-1, c].rolling(win, min_periods=0).min()
        elif c == 'c':
            df.loc[:, c] = df.loc[:, c].shift(-win).fillna(method='ffill')
        elif c == 'v':
            df.loc[::-1, c] = df.loc[::-1, c].rolling(win, min_periods=0).sum()
    return df


class DataManager:
    """ Object to manage requests to data base.

    Attributes
    ----------
    assets : str or list of str
        Id(s) of the asset(s) to requests.
    ohlcv : str or list of str
        Kind of price data to requests, following are available 'o' to open,
        'h' to high, 'l' to low, 'c' to close and 'v' to volume.
    frequency : int, optional
        Number of second between two data observations (> 60).
    path : str
        Database's path to load data.
    n_min_obs : int, optional
        Minimal number of historic data to compute signal, default is 1.

    Methods
    -------
    get_data(start=None, last=None)
        Request specified data in the data base.

    """

    def __init__(self, assets, ohlcv, frequency=60, path='data_base/',
                 n_min_obs=1):
        """ Set the data manager class.

        Parameters
        ----------
        assets : str or list of str
            Id(s) of the asset(s) to requests.
        ohlcv : str or list of str
            Kind of price data to requests, following are available 'o' to
            open, 'h' to high, 'l' to low, 'c' to close and 'v' to volume.
        frequency : int, optional
            Number of second between two data observations (> 60).
        path : str
            Database's path to load data.
        n_min_obs : int, optional
            Minimal number of historic data to compute signal, default is 1.

        """
        self.assets = assets
        self.ohlcv = ohlcv
        self.frequency = frequency
        self.path = path
        self.n_min_obs = n_min_obs

    def get_data(self, start=None, last=None):
        """ Get data from data base.

        Parameters
        ----------
        start : int, optional
            First observation to request, default is `None`.
        last : int, optional
            Last observation to request, default is `None`.

        Returns
        -------
        data : pd.DataFrame
            A data frame with the data requested.

        """
        if last is None:
            last = int(time.time() // self.frequency * self.frequency)
        else:
            last += self.frequency  # 60 is may be enought ?
        if start is None:
            start = last - self.frequency * (self.n_min_obs + 1)
        return data_base_requests(
            self.assets.copy(), self.ohlcv, self.frequency,
            start=start, end=last, path=self.path
        )


def set_dataframe(data, rename={}, index=None, drop=None):
    """ Set raw data to data frame.

    Parameters
    ----------
    data : list of list
        Raw data.
    rename : dict
        Keys are original column names and values are the new names.
    index : str
        If index is not `None` set index with `index` column name.
    drop : str or list
        Columns to drop.

    Returns
    -------
    df : pandas.DataFrame
        A dataframe.

    Examples
    --------
    >>> data = [[0, 10, 12], [1, 7, 12], [2, 9, 12]]
    >>> set_dataframe(
            data, rename={0: 'index', 1: 'price'}, index='index', drop=2
        )
           price
    index       
    0       10.0
    1        7.0
    2        9.0

    """
    df = pd.DataFrame(np.array(data, dtype=np.float64))
    df.rename(columns=rename, inplace=True)
    if index is not None:
        df.set_index(index, inplace=True)
        df.index = df.index.astype(int)
    if drop is not None:
        df.drop(columns=drop, inplace=True)
    return df


def get_ohlcv(exchange, pair, since=None, frequency=60):
    """ Requests ohlcv data from a specified exchange.

    Parameters
    ----------
    exchange : str
        Name of the exchange to request ohlcv data. Currently only kraken
        exchange allowed.
    pair : str
        Exchange's code of the pair requested.
    since : int, optional
        Timestamp of the first observation to request.
    frequency : int, optional
        Time interval in second between to frequency, minimum is 60.

    Returns
    -------
    data : dict
        Raw data.

    """
    if exchange.lower() == 'kraken':
        data = DataRequests('https://api.kraken.com/0/public/').get_data(
            'OHLC', pair=pair, interval=int(frequency / 60), since=since
        )
    else:
        raise ValueError('Unknow exchange: ', exchange)
    return data


if __name__ == '__main__':
    import doctest
    doctest.testmod()
