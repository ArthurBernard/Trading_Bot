#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-05-12 22:57:20
# @Last modified by: ArthurBernard
# @Last modified time: 2020-01-30 18:46:20

""" Client to manage a financial strategy. """

# Built-in packages
import time
import importlib
import logging
from os import getpid, getppid

# External packages

# Local packages
# from strategy_manager import DataBaseManager, DataExchangeManager
from data_requests import get_close, DataBaseManager, DataExchangeManager
from tools.time_tools import now
from tools.utils import load_config_params  # , dump_config_params, get_df
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
    name_strat : str
        Name of the strategy to run.
    _get_order_params : function
        Function to get signal strategy and additional parameters to set order.

    """

    # TODO : Load strategy config
    def __init__(self, name_strat, path, STOP=None, address=('', 50000),
                 authkey=b'tradingbot'):
        """ Initialize strategy manager.

        Parameters
        ----------
        name_strat : str
            Name of the strategy to run.
        path : str
            Path to load configuration, functions and any scripts needed to run
            the strategy.
        STOP : int, optional
            Number of iteration before stoping, if None it will load it in
            configuration file.
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
            'strategies.' + name_strat + '.strategy'
        )
        self._get_order_params = strat.get_order_params
        self.logger.info('Strategy function loaded')

        # Load configuration
        self.STOP = STOP
        self.name_strat = name_strat
        self.set_configuration(path + name_strat + '/configuration.yaml')

        self.logger.info('{} initialized'.format(name_strat))

    def set_configuration(self, path):
        """ Set configuration.

        Parameters
        ----------
        path : str
            Path to load the YAML configuration file.

        """
        self.logger.info('Load configuration of {}'.format(self.name_strat))
        data_cfg = load_config_params(path)

        # Set general parameters and strategy state
        self.underlying = data_cfg['strat_manager_instance']['underlying']
        self.iso_vol = data_cfg['strat_manager_instance']['iso_volatility']
        self.frequency = data_cfg['strat_manager_instance']['frequency']
        self.id_strat = data_cfg['strat_manager_instance']['id_strat']
        self.current_pos = data_cfg['strat_manager_instance']['current_pos']
        self.current_vol = data_cfg['strat_manager_instance']['current_vol']
        if self.STOP is None:
            self.STOP = data_cfg['strat_manager_instance']['STOP']

        # Set data manager configuration
        self.set_data_manager(**data_cfg['get_data_instance'].copy())

        # Set parameters for strategy function
        self.f_args = data_cfg['strategy_instance']['args_params']
        self.f_kwrds = data_cfg['strategy_instance']['kwargs_params']

        # Set parameters for orders
        self.ord_kwrds = data_cfg['order_instance']

        # TODO : Set ResultManager
        self.logger.info('Strategy is configured')

    def start_loop(self, condition=True):
        """ Run a loop until condition is false. """
        # test = True
        for output in self:
            # while condition:
            if output:
                self.logger.info('Executed order : {}'.format(output))

            txt = time.strftime('%y-%m-%d %H:%M:%S')
            txt += ' | Next signal in {:.1f}'.format(self.next - time.time())
            # if self.p_fees._getvalue():
            #    txt = 'Fees received | ' + txt
            #    if test:
            #        test = False
            #        self.logger.debug(self.p_fees._getvalue())

            print(txt, end='\r')
            time.sleep(0.01)

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
        self.logger.info('Bot starting | Stop in {:.0f}\'.'.format(
            self.time_stop()
        ))

        return self

    def __next__(self):
        """ Iterate method.

        Returns
        -------
        list
            If the position moved then return the list of executed orders,
            otherwise returns an empty list.

        """
        server_stop = self.is_stop()
        strat_stop = self.t >= self.STOP
        if server_stop or strat_stop:
            self.logger.info('Server stop : {}'.format(server_stop))
            self.logger.info('Strategy stop : {}'.format(strat_stop))

            raise StopIteration

        self.TS = int(time.time())

        # Sleep until ready
        # if self.next > self.TS:
        #    time.sleep(self.next - self.TS)
        if self.next <= self.TS:

            self.next += self.frequency
            self.t += 1
            self.logger.info('{}th iter. over {}'.format(self.t, self.STOP))
            self.logger.info('Stop in {:.0f}\'.'.format(self.time_stop()))

            # TODO : Debug/find solution to request data correctly.
            #        Need to choose between request a database, server,
            #        exchange API or other.
            data = self.DM.get_data(*self.args_data, **self.kwargs_data)

            return self.get_order_params(data)

        return []

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
        out = []
        signal, kw = self._get_order_params(data, *self.f_args, **self.f_kwrds)

        # Don't move
        if self.current_pos == signal:
            self.logger.info('Position not moved')

            return []

        # Up move
        elif self.current_pos <= 0. and signal >= 0:
            kw['type'] = 'buy'
            out += self.cut_short(signal, **kw.copy(), **self.ord_kwrds.copy())
            out += self.set_long(signal, **kw.copy(), **self.ord_kwrds.copy())

        # Down move
        elif self.current_pos >= 0. and signal <= 0:
            kw['type'] = 'sell'
            out += self.cut_long(signal, **kw.copy(), **self.ord_kwrds.copy())
            out += self.set_short(signal, **kw.copy(), **self.ord_kwrds.copy())

        return out

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
            self.logger.info('Short position cutted')

        else:
            result = self._set_output(kwargs)

        return [result]

    def set_long(self, signal, **kwargs):
        """ Set long order. """
        if signal > 0:
            result = self.send_order(**kwargs)

            # Set current volume
            self.current_vol = kwargs['volume']
            self.current_pos = signal
            result['current_volume'] = self.current_vol
            result['current_position'] = signal
            self.logger.info('Long position placed')

        else:
            result = self._set_output(kwargs)

        return [result]

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
            self.logger.info('Long position cutted')

        else:
            result = self._set_output(kwargs)

        return [result]

    def set_short(self, signal, **kwargs):
        """ Set short order. """
        if signal < 0:
            # Set leverage to short
            leverage = kwargs.pop('leverage')
            kwargs['leverage'] = 2 if leverage is None else leverage + 1
            result = self.send_order(**kwargs)

            # Set current volume
            self.current_vol = kwargs['volume']
            self.current_pos = signal
            result['current_volume'] = self.current_vol
            result['current_position'] = signal
            self.logger.info('Short position placed')

        else:
            result = self._set_output(kwargs)

        return [result]

    def send_order(self, **kwargs):
        """ Send the ID of strategy and order parameters to OrdersManager. """
        self.q_ord.put((self.id_strat, kwargs))
        self.logger.debug(
            'Set order | Strat {} | Params {}'.format(self.id_strat, kwargs)
        )

        return self._set_output(kwargs)

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
            # TODO : Improve it
            #    request close price to OM ? TBM ? DM ?
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

        self.logger.info('DataManager initialized')

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

    sm = StrategyManager('another_example_2', './strategies/')
    sm.start_loop()
