#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import yaml

# Import external packages


__all__ = ['load_config_params']


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
    {'strat_manager_instance': {'strat_name': 'example_function', 'underlying': 'example_coin', 'frequency': 60, 'path': '/home/user/path/folder/subfolder/'}, 'extra_instance': {'time_exec': 0, 'request_data_on_the_flye': True}, 'args_params': ['para1', 'para2'], 'kwargs_params': {'para1': 0, 'para2': 1}}

    """
    with open(path, 'r') as f:
        data = yaml.load(f)
    return data


if __name__ == '__main__':
    import doctest
    doctest.testmod()