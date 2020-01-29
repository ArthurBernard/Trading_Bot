#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-05-12 22:57:20
# @Last modified by: ArthurBernard
# @Last modified time: 2020-01-29 21:32:19

""" Client to manage a financial strategy. """

# Built-in packages
import time
import importlib
import logging
from os import getpid, getppid

# External packages

# Local packages
# from strategy_manager.tools.time_tools import now
# from strategy_manager import DataBaseManager, DataExchangeManager
# from strategy_manager.data_requests import get_close
from _client import _BotClient

__all__ = ['StrategyManager']


class StrategyManager(_BotClient):
    """ Main object to load data, compute signals and execute orders.

    Methods
    -------
    get_order_params(data)
        Computes and returns signal strategy and additional parameters to set
        order.
    __call__(*args, **kwargs)
        Set parameters to pass into function' strategy.
    set_iso_vol(series, target_vol=0.2, leverage=1., period=252, half_life=11)
        Computes and returns iso-volatility coefficient.

    Attributes
    ----------
    frequency : int
        Number of seconds between two samples.
    underlying : str
        Name of the underlying or list of data needed.
    _get_order_params : function
        Function to get signal strategy and additional parameters to set order.

    """
    # TODO : Load strategy config

    def __init__(self, frequency, underlying, script_name, current_pos,
                 current_vol, STOP=None, iso_volatility=True,
                 address=('', 50000), authkey=b'tradingbot'):
        """ Initialize strategy manager.

        Parameters
        ----------
        frequency : int
            Number of seconds between two samples.
        underlying : str
            Name of the underlying or list of data needed.
        script_name : str
            Name of script to load function strategy (named `get_signal`).
        STOP : int, optional
            Number of iteration before stoping, if None iteration will
            stop at the end of the day (UTC time). Default is None.
        iso_volatility : bool, optional
            If true apply a coefficient of money management computed from
            underlying volatility. Default is True.

        """
        # Set client and connect to the trading bot server
        _BotClient.__init__(self, address=address, authkey=authkey)

        self.logger = logging.getLogger('trad_bot.' + __name__)
        self.logger.info('Initialize StrategyManager | Current PID is '
                         '{} and Parent PID is {}'.format(getpid(), getppid()))

        # Import strategy
        strat = importlib.import_module(
            'strategies.' + script_name + '.strategy'
        )
        self._get_order_params = strat.get_order_params
        self.logger.info('Strategy function loaded')

        # Load configuration
        # TODO : load strat_manager_instance
        self.frequency = frequency
        self.underlying = underlying
        self.current_pos = current_pos
        self.current_vol = current_vol
        self.t = 0
        self.STOP = STOP
        self.iso_vol = iso_volatility
        self.logger.info('Configuration loaded')

    def start_loop(self, condition=True):
        """ Run a loop until condition is false. """
        self.logger.info('Bot is starting, it will stop in {:.0f}\'.'.format(
            self.time_stop()
        ))
        test = True
        while condition:
            txt = time.strftime('%y-%m-%d %H:%M:%S')
            if self.p_fees._getvalue():
                txt = 'Fees received | ' + txt
                if test:
                    test = False
                    self.logger.debug(self.p_fees._getvalue())

            print(txt, end='\r')
            time.sleep(0.1)

            if self.is_stop():
                break

        self.logger.info('StrategyManager stopped.')

    def __call__(self, *args, **kwargs):
        """ Set parameters of strategy.

        Parameters
        ----------
        args : tuple, optional
            Any arguments to pass into function' strategy.
        kwargs : dict, optionl
            Any keyword arguments to pass into function' strategy.

        Returns
        -------
        StrategyManager
            Object to manage strategy computations.

        """
        self.args = args
        self.kwargs = kwargs

        return self

    def __iter__(self):
        """ Initialize iterative method. """
        self.t = 0
        self.TS = now(self.frequency)
        self.next = self.TS + self.frequency

        return self

    def __next__(self):
        """ Iterate method. """
        # if self.t >= self.STOP:
        if self.is_stop():
            raise StopIteration

        self.TS = int(time.time())

        # Sleep until ready
        if self.next > self.TS:
            time.sleep(self.next - self.TS)

        self.next += self.frequency
        self.t += 1
        self.logger.info('{}th iteration over {}'.format(self.t, self.STOP))
        self.logger.info('Bot will stop in {:.0f}\'.'.format(self.time_stop()))

        # TODO : Debug/find solution to request data correctly.
        #        Need to choose between request a database, server,
        #        exchange API or other.
        data = self.DM.get_data(*self.args_data, **self.kwargs_data)

        return self.get_order_params(data)

    def get_order_params(self, data):
        """ Compute signal and additional parameters to set order.

        Parameters
        ----------
        data : pandas.DataFrame
            Data to compute signal' strategy.

        Returns
        -------
        signal : float
            Signal of strategy.
        params : tuple
            Parameters for order, e.g. volume, order type, etc.

        """
        signal, kw = self._get_order_params(data, *self.args, **self.kwargs)

        # Don't move
        if self.current_pos == signal:

            return None

        # Up move
        elif self.current_pos <= 0. and signal >= 0:
            kw['type'] = 'buy'
            self.set_order(self.cut_short(signal, **kw.copy()))
            self.set_order(self.set_long(signal, **kw.copy()))

        # Down move
        elif self.current_pos >= 0. and signal <= 0:
            kw['type'] = 'sell'
            self.cut_long(signal, **kw.copy())
            self.set_short(signal, **kw.copy())

        return None

    def cut_short(self, signal, **kwargs):
        """ Cut short position. """
        if self.current_pos < 0:
            # Set leverage to cut short
            leverage = kwargs.pop('leverage')
            kwargs['leverage'] = 2 if leverage is None else leverage + 1

            # Set volume to cut short
            kwargs['volume'] = self.current_vol

            # Query order
            result = self.send_order(**kwargs)

            # Set current volume and position
            self.current_vol = 0.
            self.current_pos = 0
            result['current_volume'] = 0.
            result['current_position'] = 0

        else:
            result = self._set_output(kwargs)

        return result

    def set_long(self, signal, **kwargs):
        """ Set long order. """
        if signal > 0:
            result = self.send_order(**kwargs)

            # Set current volume
            self.current_vol = kwargs['volume']
            self.current_pos = signal
            result['current_volume'] = self.current_vol
            result['current_position'] = signal

        else:
            result = self._set_output(kwargs)

        return result

    def cut_long(self, signal, **kwargs):
        """ Cut long position. """
        if self.current_pos > 0:
            # Set volume to cut long
            kwargs['volume'] = self.current_vol

            # Query order
            result = self.send_order(**kwargs)

            # Set current volume
            self.current_vol = 0.
            self.current_pos = 0
            result['current_volume'] = 0.
            result['current_position'] = 0

        else:
            result = self._set_output(kwargs)

        return result

    def set_short(self, signal, **kwargs):
        """ Set short order. """
        if signal < 0:
            # Set leverage to short
            leverage = kwargs.pop('leverage')
            kwargs['leverage'] = 2 if leverage is None else leverage + 1
            result = self.send_order(leverage=leverage, **kwargs)

            # Set current volume
            self.current_vol = kwargs['volume']
            self.current_pos = signal
            result['current_volume'] = self.current_vol
            result['current_position'] = signal

        else:
            result = self._set_output(kwargs)

        return result

    def _set_output(self, kwargs):
        """ Set output when no orders query. """
        result = {
            'timestamp': now(self.frequency),
            'current_volume': self.current_vol,
            'current_position': self.current_pos,
            'fee': self.get_fee(kwargs['pair'], kwargs['ordertype']),
            'descr': None,
        }
        if kwargs['ordertype'] == 'limit':
            result['price'] = kwargs['price']

        elif kwargs['ordertype'] == 'market':
            result['price'] = get_close(kwargs['pair'])

        else:
            raise ValueError(
                'Unknown order type: {}'.format(kwargs['ordertype'])
            )

        return result

    def set_data_manager(self, **kwargs):
        """ Set `DataManager` object.

        Parameters
        ----------
        kwargs : dict
            Cf `DataManager` constructor.

        """
        if 'args' in kwargs.keys():
            self.args_data = kwargs.pop('args')
        else:
            self.args_data = ()

        if 'kwargs' in kwargs.keys():
            self.kwargs_data = kwargs.pop('kwargs')
        else:
            self.kwargs_data = {}

        request_from = kwargs.pop('source_data').lower()

        if request_from == 'exchange':
            self.DM = DataExchangeManager(**kwargs)
        elif request_from == 'database':
            self.DM = DataBaseManager(**kwargs)
        else:
            raise ValueError('request_data must be exchange or database.'
                             'Not {}'.format(request_from))

        return self

    def time_stop(self):
        """ Compute the number of seconds before stopping.

        Returns
        -------
        int
            Number of seconds before stopping.

        """
        time_stop = (self.STOP - self.t) * self.frequency

        return max(time_stop - time.time() % self.frequency, 0)


if __name__ == '__main__':

    import logging.config
    import yaml

    with open('./trading_bot_manager/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    sm = StrategyManager(60, 'XETHZUSD', 'another_example', 0, 0, STOP=100)
    sm.start_loop()
