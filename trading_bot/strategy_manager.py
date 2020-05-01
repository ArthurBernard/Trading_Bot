#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-05-12 22:57:20
# @Last modified by: ArthurBernard
# @Last modified time: 2020-05-01 16:24:58

""" Client to manage a financial strategy. """

# Built-in packages
import importlib
import logging
from pickle import Pickler, Unpickler
import sys
from threading import Thread
import time

# External packages

# Local packages
from trading_bot._client import _ClientStrategyBot
# from trading_bot._containers import OrderDict
from trading_bot.data_requests import get_close
from trading_bot.orders import OrderSL, OrderBestLimit
# from trading_bot.performance import PnL
from trading_bot.tools.io import load_config_params, dump_config_params
from trading_bot.tools.time_tools import now, str_time

__all__ = ['StrategyBot']


class StrategyBot(_ClientStrategyBot):
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
    get_order_params(data, *args, **kwargs)
        Function to get signal strategy and additional parameters to set order.
        `data` must be a pd.DataFrame, `*args` and `**kwargs` can be any
        parameters.

    Attributes
    ----------
    get_order_params : callable
        Strategy function that returns a tuple with a signal ({1, 0, -1}) and
        additional parameters (dict) to set order.
    name_strat : str
        Name of the strategy to run.
    STOP : int, optional
        Number of iteration before stoping, if None it will load it in
        configuration file.
    path : str, optional
        Path of the folder to load the strategy and configuration file.
    frequency : int
        Number of seconds between two samples.

    """

    _handler_order = {
        'submit_and_leave': OrderSL,
        'best_limit': OrderBestLimit,
    }
    _handler_pos = ['neutral', 'long', 'short']
    order_sent = []
    pnl = None

    # TODO : Load strategy config
    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize strategy manager.

        Parameters
        ----------
        address :
        authkey :

        """
        # Set client and connect to the trading bot server
        _ClientStrategyBot.__init__(self, address=address, authkey=authkey)
        self.logger = logging.getLogger('strategy_bot')

    def __call__(self, name_strat, STOP=None, path='./strategies'):
        """ Set parameters of strategy.

        Parameters
        ----------
        name_strat : str
            Name of the strategy to run.
        STOP : int, optional
            Number of iteration before stoping, if None it will load it in
            configuration file.
        path : str, optional
            Path of the folder to load the strategy.

        Returns
        -------
        StrategyBot
            Object to manage strategy computations.

        """
        conf = load_config_params('./general_config.yaml')
        strat_path = conf['path']['strategy']
        if strat_path[-1] != '/':
            strat_path += '/'

        # Import strategy
        spec = importlib.util.spec_from_file_location(
            'strategies.' + name_strat + '.strategy',
            strat_path + name_strat + '/strategy.py'
        )
        strat_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(strat_module)

        self.get_order_params = strat_module.get_order_params
        self.logger.info('Load {} strategy function'.format(name_strat))

        self.name_strat = name_strat
        self.STOP = STOP
        self.path = path + '/' + name_strat

        return self

    def __iter__(self):
        """ Initialize iterative method. """
        self.t = 0
        self.TS = now(self.frequency)
        self.next = self.TS + self.frequency
        self.logger.info('Start now and stop in {}'.format(
            str_time(int(self.time_stop()))
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
            self.logger.debug('Server stop : {}'.format(server_stop))
            self.logger.debug('Strategy stop : {}'.format(strat_stop))

            raise StopIteration

        self.TS = int(time.time())  # + 7200  # force to send signal
        if self.next <= self.TS:
            self.next += self.frequency
            self.t += 1
            self.logger.info('{}/{}iteration, stop in {}'.format(
                self.t, self.STOP, str_time(int(self.time_stop()))
            ))
            # TODO : Debug/find solution to request data correctly.
            #        Need to choose between request a database, server,
            #        exchange API or other.
            data = self.DM.get_data(*self.args_data, **self.kwargs_data)

            return self.get_order_params(data, *self.f_args, **self.f_kwrds)

        time.sleep(0.01)

        return None, None

    def __enter__(self):
        """ Enter. """
        # TODO : Load precedent data
        self.logger.info('Load configuration')
        self.set_config(self.path + '/configuration.yaml')
        super(StrategyBot, self).__enter__()
        self.conn_tbm.thread = Thread(target=self.listen_tbm, daemon=True)
        self.conn_tbm.thread.start()
        # send name of strategy to TBM
        self.conn_tbm.send(('name', self.name_strat),)
        # TODO : load history ? Is it necessary ?
        # self.get_histo_orders(self.path + '/orders_hist.dat')
        # self.get_histo_result(self.path + '/result_hist.dat')

        # Send info to compute PnL
        self.q_tpm.put({
            'path': self.path,
            'timestep': self.frequency,
            'real': not self.ord_kwrds.get('validate', False),
        })

        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        """ Exit. """
        # wait until received all orders closed
        # if not self.is_stop():
        #    self._wait_orders_closed()

        self.logger.info('Save configuration')
        # Save configuration and data
        self.set_general_cfg(self.path + '/configuration.yaml')
        # TODO: Save history ? Only if loaded it is necessary
        # self.set_histo_orders(self.path + '/orders_hist.dat')
        # self.set_histo_result(self.path + '/result_hist.dat')

        # with open(self.path + '/pending_orders.dat', 'wb') as f:
        #    Pickler(f).dump(self.order_sent)

        if exc_type is not None:
            self.logger.error(
                '{}: {}'.format(exc_type, exc_value),
                exc_info=True
            )

        self.logger.info('end')
        super(StrategyBot, self).__exit__(exc_type, exc_value, exc_tb)
        self.conn_tbm.thread.join()

    def set_config(self, path):
        """ Set configuration.

        Parameters
        ----------
        path : str
            Path to load the YAML configuration file.

        """
        self.logger.info('set_config Strat {}'.format(self.name_strat))
        self.cfg = load_config_params(path)
        # Set general configuration
        self._get_general_cfg(self.cfg['strat_manager_instance'])
        # Set data manager configuration
        self.set_data_manager(**self.cfg['get_data_instance'].copy())
        # Set stategy parameters
        self._strat_cfg(self.cfg['strategy_instance'])
        # Set parameters for orders
        self.ord_kwrds = self.cfg['order_instance']
        # Set parameters display results
        self.result_kwrds = self.cfg['result_instance']
        # TODO : Set ResultManager
        self.logger.info('set_config | Strategy is configured')

    def _get_general_cfg(self, strat_cfg):
        # Get general parameters and strategy state
        self.frequency = strat_cfg['frequency']
        self.id = strat_cfg['id_strat']
        _id = '0' * (3 - len(str(self.id))) + str(self.id)
        name_logger = 'strategy_bot.n-' + _id
        self.logger = logging.getLogger(name_logger)
        self.current_pos = strat_cfg['current_pos']
        self.current_vol = strat_cfg['current_vol']
        self.Order = self._handler_order[strat_cfg['order']]
        self.reinvest = strat_cfg['reinvest']
        self.logger.info('current position is {}'.format(self.current_pos))
        self.logger.info('current volume is {}'.format(self.current_vol))
        if self.STOP is None:
            self.STOP = strat_cfg['STOP']

        # FIXME : why ?
        elif isinstance(self.STOP, int):
            self.logger.info('Strategy will never stop')
            self.STOP = 1e8

    def set_general_cfg(self, path):
        """ Save configuration with respect to new position and volume.

        Parameters
        ----------
        path : str
            path to save the configuration file.

        """
        self.cfg['strat_manager_instance']['current_pos'] = self.current_pos
        self.cfg['strat_manager_instance']['current_vol'] = self.current_vol
        self.logger.info('current position is {}'.format(self.current_pos))
        self.logger.info('current volume is {}'.format(self.current_vol))
        dump_config_params(self.cfg, path)

    def _strat_cfg(self, strat_cfg):
        # Set parameters for strategy function
        self.f_args = strat_cfg['args_params']
        self.f_kwrds = strat_cfg['kwargs_params']

    def set_order(self, s, **kwargs):
        """ Compute signal and additional parameters to set order.

        Parameters
        ----------
        s : {-1, 0, 1}
            Signal of strategy.
        **kwargs : keyword arguments
            Parameters for order, e.g. volume, order type, etc.

        Returns
        -------
        list or float
            If one or several orders are sent, returns list with information
            about them, otherwise returns the current price of underlying.

        """
        out = []
        # Up move
        if self.current_pos <= 0. and s >= 0 and self.current_pos != s:
            kwargs['type'] = 'buy'
            if self.current_pos < 0:
                out += self._cut_short(s, **kwargs.copy())

            if s > 0:
                out += self._set_long(s, **kwargs.copy())

        # Down move
        elif self.current_pos >= 0. and s <= 0 and self.current_pos != s:
            kwargs['type'] = 'sell'
            if self.current_pos > 0:
                out += self._cut_long(s, **kwargs.copy())

            if s < 0:
                out += self._set_short(s, **kwargs.copy())

        if not out:

            return self._set_output(kwargs)['price']

        return out

    def _cut_short(self, signal, **kwargs):
        """ Cut short position. """
        # Set leverage to cut short
        leverage = kwargs.pop('leverage')
        kwargs['leverage'] = 2 if leverage is None else leverage + 1

        # Set volume to cut short
        kwargs['volume'] = self.current_vol

        # Query order
        result = self.send_order(**kwargs)

        # Set current volume and position
        self.current_vol = 0.
        self.current_pos = 0.
        result['current_volume'] = 0.
        result['current_position'] = 0.
        self.logger.info('_cut_short | pos: {}'.format(self.current_pos))

        return [result]

    def _set_long(self, signal, **kwargs):
        """ Set long order. """
        # Set volume if reinvest profit
        if self.reinvest:
            kwargs['volume'] = self.get_current_volume(kwargs['volume'])

        result = self.send_order(**kwargs)

        # Set current volume
        self.current_vol = kwargs['volume']
        self.current_pos = float(signal)
        result['current_volume'] = self.current_vol
        result['current_position'] = signal
        self.logger.info('_set_long | pos: {}'.format(self.current_pos))

        return [result]

    def _cut_long(self, signal, **kwargs):
        """ Cut long position. """
        # Set volume to cut long
        kwargs['volume'] = self.current_vol

        # Query order
        result = self.send_order(**kwargs)

        # Set current volume
        self.current_vol = 0.
        self.current_pos = 0.
        result['current_volume'] = 0.
        result['current_position'] = 0.
        self.logger.info('_cut_long | pos: {}'.format(self.current_pos))

        return [result]

    def _set_short(self, signal, **kwargs):
        """ Set short order. """
        # Set leverage to short
        leverage = kwargs.pop('leverage')
        kwargs['leverage'] = 2 if leverage is None else leverage + 1

        # Set volume if reinvest profit
        if self.reinvest:
            kwargs['volume'] = self.get_current_volume(kwargs['volume'])

        result = self.send_order(**kwargs)

        # Set current volume
        self.current_vol = kwargs['volume']
        self.current_pos = float(signal)
        result['current_volume'] = self.current_vol
        result['current_position'] = signal
        self.logger.info('_set_short | pos: {}'.format(self.current_pos))

        return [result]

    def get_current_volume(self, volume):
        """ Get the current volume available.

        Parameters
        ----------
        volume : float
            Last folume available.

        Returns
        -------
        float
            New current volume available.

        """
        path = self.path
        if path[-1] != '/':
            path += '/'

        try:
            with open(path + 'current_volume.dat', 'rb') as f:
                self.logger.debug('load current volume')

                return Unpickler(f).load()

        except FileNotFoundError:
            self.logger.error('file not found to load current volume')

            return volume

    def send_order(self, **kwargs):
        """ Send the ID of strategy and order parameters to OrdersManager.

        Parameters
        ----------
        **kwargs : keyword arguments
            Parameters for order, e.g. volume, order type, etc.

        Returns
        -------
        dict
            Information about the executed order.

        """
        # Set order parameters
        _id = self._set_id_order()
        info = {
            'fee_pct': self.get_fee(kwargs['pair'], kwargs['ordertype']),
            'ex_pos': self.current_pos,
            'ex_vol': self.current_vol,
            'strat_id': self.id,
            'strat_name': self.name_strat,
            'path': self.path,
            'TS': self.next - self.frequency
        }
        order_params = kwargs.pop('order_params')
        if order_params is None:
            order_params = {}

        # Set order
        order = self.Order(_id, input=kwargs, info=info, **order_params)
        # Send order to OrdersManager
        self.q_ord.put(order)
        self.order_sent += [_id]
        self.logger.info('send {}'.format(order))

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
        self.DM = self._handler[request_from](**kwargs)
        self.logger.info('Initialize {}'.format(request_from))

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

    def start_loop(self):
        """ Run a loop until condition is false. """
        for s, kw in self:
            self.process_signal(s, kw)

        self.logger.info('StrategyBot stopped.')

    def process_signal(self, s, kw):
        """ Process a signal. """
        if s is None:

            return

        self.logger.info('Signal: {} | Parameters: {}'.format(s, kw))
        output = self.set_order(s, **kw, **self.ord_kwrds)
        if isinstance(output, list):
            self.logger.info('Executed order : {}'.format(output))
            price = output[0]['price']

        else:
            price = output

        TS = self.next - self.frequency
        with open(self.path + '/price.txt', 'a') as f:
            f.write(str(TS) + ',' + str(price) + '\n')

        if not isinstance(output, list):
            # Send info to compute PnL
            self.q_tpm.put({
                'path': self.path,
                'timestep': self.frequency,
                'real': not self.ord_kwrds.get('validate', False),
            })

    def _set_id_order(self, n=3):
        r""" Set an unique order identifier.

        Parameters
        ----------
        n : int
            $10^n$ is the maximum number of different ID strategies allowed. By
            default `n=3` such that the number of different strategies will not
            exceed 1000.

        Returns
        -------
        id_order : int (signed and 32-bit)
            Number to identify an order and link it with a strategy.

        """
        s = 10 ** n
        try:
            with open(self.path + '/id_order.dat', 'rb') as f:
                id_order = Unpickler(f).load()

        except FileNotFoundError:
            id_order = 0

        id_order += 1
        if id_order > 2147483647 // s:
            id_order = 0

        with open(self.path + '/id_order.dat', 'wb') as f:
            Pickler(f).dump(id_order)

        id_strat = '0' * (n - len(str(self.id))) + str(self.id)

        return int(str(id_order) + str(id_strat))

    def listen_tbm(self):
        """ Wait message from TBM. """
        self.logger.debug('starting listen tbm')
        for k, a in self.conn_tbm:
            self._handler_tbm(k, a)
            if self.is_stop():
                self.conn_tbm.shutdown()

        self.logger.debug('stopping listen tbm')

    def _handler_tbm(self, k, a):
        if k is None:
            pass

        elif k == 'order':
            if a in self.order_sent:
                # remove order of pending orders list
                self.order_sent.remove(a)

            # if pending orders list is empty
            if not self.order_sent:
                # Send info to compute PnL
                self.q_tpm.put({
                    'path': self.path,
                    'timestep': self.frequency,
                    'real': not self.ord_kwrds.get('validate', False),
                })

        elif k == '_stop':
            # enforce stop time
            self.logger.info('TradingBotManager sent a STOP command')
            self.t = self.STOP

        else:
            self.logger.error('received unknown message {}: {}'.format(k, a))


if __name__ == '__main__':

    import logging.config

    # Load logging configuration
    log_config = load_config_params('./trading_bot/logging.ini')
    logging.config.dictConfig(log_config)

    if len(sys.argv) < 2:
        print('/!\\ RUN ANOTHER_EXAMPLE /!\\')
        name = 'another_example'

    else:
        name = sys.argv[1]

    # Start running a strategy bot
    sm = StrategyBot()
    with sm(name):
        for s, kw in sm:
            sm.process_signal(s, kw)
            txt = time.strftime('%y-%m-%d %H:%M:%S')
            txt += ' | Next signal in {:}'.format(str_time(sm.next - sm.TS))
            print(txt, end='\r')
            time.sleep(0.01)

        sm.logger.info('StrategyBot stopped.')

    print('bye')
