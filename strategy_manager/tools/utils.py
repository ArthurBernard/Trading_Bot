#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import yaml
from pickle import Pickler, Unpickler

# Import external packages
import pandas as pd


__all__ = ['load_config_params', 'save_df', 'get_df']


def load_config_params(path):
    """ Function to load configuration parameters for strategy manager.

    Parameters
    ----------
    path : str
        File's path to load configuration parameters.

    Returns
    -------
    dict
        Parameters.

    Examples
    --------
    >>> load_config_params('strategies/example_function.cfg')
    {'strat_manager_instance': {'strat_name': 'example_function', 'underlying': 'example_coin', 'frequency': 60, 'volume': 0}, 'extra_instance': {'time_exec': 0, 'request_data_on_the_flye': True}, 'args_params': ['para1', 'para2'], 'kwargs_params': {'para1': 0, 'para2': 1}}

    """
    with open(path, 'r') as f:
        data = yaml.load(f)
    return data


def get_df(path, name, ext=''):
    """ Load a dataframe as binnary file.

    Parameters
    ----------
    path, name, ext : str
        Path to the file, name of the file and the extension of the file.

    Returns
    -------
    df : pandas.DataFrame
        A dataframe, if file not find return an empty dataframe.

    """
    if path[-1] != '/' and name[0] != '/':
        path += '/'
    if len(ext) > 0 and ext[0] != '.':
        ext = '.' + ext
    try:
        with open(path + name + ext, 'rb') as f:
            df = Unpickler(f).load()
            return df
    except FileNotFoundError:
        return pd.DataFrame()


def save_df(df, path, name, ext=''):
    """ Save a dataframe as a binnary file.

    Parameters
    ----------
    df : pandas.DataFrame
        A dataframe to save as binnary file.
    path, name, ext : str
        Path to the file, name of the file and the extension of the file.

    """
    if path[-1] != '/' and name[0] != '/':
        path += '/'
    if len(ext) > 0 and ext[0] != '.':
        ext = '.' + ext
    with open(path + name + ext, 'wb') as f:
        Pickler(f).dump(df)


if __name__ == '__main__':
    import doctest
    doctest.testmod()
