#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-03-08 20:19:22
# @Last modified by: ArthurBernard
# @Last modified time: 2020-01-14 10:47:22

""" Some utility functions. """

# Built-in packages
from pickle import Pickler, Unpickler
from os import makedirs

# External packages
from ruamel.yaml import YAML
import pandas as pd


__all__ = ['load_config_params', 'dump_config_params', 'save_df', 'get_df']


def load_config_params(path):
    """ Load configuration parameters for strategy manager.

    Parameters
    ----------
    path : str
        File's path to load configuration parameters.

    Returns
    -------
    dict
        Parameters.

    """
    yaml = YAML()

    with open(path, 'r') as f:
        data = yaml.load(f)

    return data


def dump_config_params(data_cfg, path):
    """ Dump configuration parameters for strategy manager.

    Parameters
    ----------
    data : list or dict
        Data configuration to dump in yaml format.
    path : str
        File's path to dump configuration parameters.

    """
    yaml = YAML()

    with open(path, 'w') as f:
        yaml.dump(data_cfg, f)


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

    try:
        with open(path + name + ext, 'wb') as f:
            Pickler(f).dump(df)

    except FileNotFoundError:
        makedirs(path, exist_ok=True)

        with open(path + name + ext, 'wb') as f:
            Pickler(f).dump(df)


if __name__ == '__main__':

    import doctest

    doctest.testmod()
