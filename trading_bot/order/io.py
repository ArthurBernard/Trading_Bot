#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-25 14:09:58
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-26 17:00:51

""" Transforms order into recordable format. """

# Built-in packages
import time

# Third party packages
import pandas as pd

# Local packages
from trading_bot.tools.io import get_df, save_df


COLUMNS = ['userref', 'txid', 'price', 'volume', 'pair', 'type', 'price_exec',
           'vol_exec', 'cost', 'ordertype', 'leverage', 'start_time',
           'end_time', 'fee', 'feeq', 'feeb', 'fee_pct', 'strat_id']


def set_df_from_order(order):
    """ Set a dataframe with the main information from a closed order.

    Parameters
    ----------
    order : Order
        An order object.

    Returns
    -------
    pandas.DataFrame
        Main order information.

    """
    result = set_dict_from_order(order)
    df = pd.DataFrame([result])
    # df.loc[:, COLUMNS] = df.loc[:, COLUMNS]

    return df
    # return pd.DataFrame([result], columns=COLUMNS)


def update_df_from_order(order, path='.', name='orders_hist', ext='.dat'):
    """ Update a dataframe with the main information from a closed order.

    Parameters
    ----------
    order : Order
        An order object.

    """
    # TODO : Save by year ? month ? day ?
    # TODO : Don't save per strategy ?
    if path[-1] != '/':
        path += '/'

    # Get order historic dataframe
    df_hist = get_df(path, name, ext)

    # Set new order historic dataframe
    df_hist = df_hist.append(set_df_from_order(order), sort=False)
    df_hist = df_hist.reset_index(drop=True)
    if (df_hist.columns[:len(COLUMNS)] != COLUMNS).any():
        print('DATAFRAME NOT SORTED CORRECTLY')
        other_columns = list(df_hist.columns.difference(COLUMNS))
        df_hist = df_hist.reindex(columns=(COLUMNS + other_columns))

    # Save order historic dataframe
    save_df(df_hist, path, name, ext)


def set_dict_from_order(order):
    """ Set a dictionary with the main information from a closed order.

    Parameters
    ----------
    order : Order
        An order object.

    Returns
    -------
    dict
        Main order information.

    """
    result = order.result_exec
    result.update({
        'userref': order.id,
        'type': order.type,
        'price': order.price,
        'volume': order.volume,
        'pair': order.pair,
        'ordertype': order.input['ordertype'],
        'leverage': order.input['leverage'],
        'end_time': int(time.time()),
        # 'fee_pct': order.fee,
        # 'strat_id': _get_id_strat(order.id),
    })
    result.update(order.info)

    return result


def _get_id_strat(id_order, n=3):
    return int(str(id_order)[-n:])
