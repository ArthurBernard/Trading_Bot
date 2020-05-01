#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 09:58:03
# @Last modified by: ArthurBernard
# @Last modified time: 2020-05-01 11:26:43

""" Set a server and run each bot. """

# Built-in packages
import logging
from multiprocessing import Process
from threading import Thread
import time

# Third party packages

# Local packages
from trading_bot._server import _TradingBotManager
from trading_bot.strategy_manager import StrategyBot as SB
from trading_bot.tools.io import load_config_params
from trading_bot.tools.time_tools import str_time

__all__ = [
    'TradingBotManager', 'start_order_manager',
]


class TradingBotManager(_TradingBotManager):
    """ Trading Bot Manager object. """

    # TODO : load general config
    # TODO : run strategies
    # fees = {}
    # balance = {}
    _handler_om = {
        # 'fees': fees.update,
        # 'balance': balance.update,
        # 'closed_order': set_closed_order,
        # 'order': lambda x: x,
    }
    _handler_sb = {
        # 'switch_id': _TradingBotManager.conn_sb.switch_id,
    }
    process_sb = {}

    def __init__(self, address=('', 50000), authkey=b'tradingbot', auto=False):
        """ Initialize Trading Bot Manager object. """
        _TradingBotManager.__init__(self, address=address, authkey=authkey)

        self.logger = logging.getLogger(__name__)

        conf = load_config_params('./general_config.yaml')
        self.path_log = conf['path']['log_file']
        self.address = address
        self.authkey = authkey
        self.txt = {}
        self.auto = auto
        self.client_thread = Thread(target=self.client_manager, daemon=True)

    def __enter__(self):
        """ Enter into TradingBotManager context manager. """
        super(TradingBotManager, self).__enter__()
        time.sleep(1)
        self.client_thread.start()

    def __exit__(self, exc_type, exc_value, exc_tb):
        """ Exit from TradingBotManager context manager. """
        super(TradingBotManager, self).__exit__(exc_type, exc_value, exc_tb)
        if exc_type is not None:
            self.logger.error(
                '{}: {}'.format(exc_type, exc_value),
                exc_info=True
            )

        self.logger.info('TBM stopped')

    def __iter__(self):
        """ Iterative method. """
        return self

    def __next__(self):
        """ Loop until state server stop. """
        time.sleep(0.01)
        if self.is_stop():

            raise StopIteration

        return None

    def __repr__(self):
        return 'order bot is {} | {} are running'.format(
            self.order_bot, self.strat_bot
        )

    def runtime(self):
        """ Do something. """
        # TODO : Run OrderManagerClient object
        # TODO : Run all StrategyManagerClient objects
        self.logger.debug('start runtime loop')
        t0 = int(time.time())
        # while time.time() - self.t < s:
        for _ in self:
            # print('{:.1f} sec.'.format(time.time() - self.t), end='\r')
            txt = '{} | Have been started {} ago'.format(
                time.strftime('%y-%m-%d %H:%M:%S'),
                str_time(int(time.time() - t0)),
            )
            # txt += ' | Stop in {} seconds'.format(
            #    str_time(t + s - int(time.time()))
            # )
            print(txt, end='\r')
            # time.sleep(0.01)

        self.logger.debug('run | end to do something')

        # Wait until child process closed
        # p_om.join()

    def run_strategy(self, name):
        # /!\ DEPRECATED /!\
        # TODO : set a dedicated pipe
        # TODO : run a new process for a new strategy
        with SB(name, address=self.address, authkey=self.authkey) as sm:
            sm.set_configuration(self.path + name + '/configuration.yaml')
            for s, kw in sm:
                if s is not None:
                    self.logger.info('Signal: {} | Params: {}'.format(s, kw))
                    output = sm.set_order(s, **kw, **sm.ord_kwrds)

                    if output:
                        self.logger.info('executed order : {}'.format(output))

                self.txt[name] += '{} | Next signal in {}'.format(
                    name, str_time(int(sm.next - sm.TS))
                )

    def listen_om(self):
        """ Update fees and balance when received them from OrdersManager. """
        self.logger.debug('start listen OrderManager')
        for k, a in self.conn_om:
            if k in self._handler_om.keys():
                self._handler_om[k](a)
                self.logger.debug('{}: {}'.format(k, a))

            elif k == 'fees':
                self.state['fees'].update(a)
                self.logger.debug('recv {}: {}'.format(k, type(a)))

            elif k == 'balance':
                self.state['balance'].update(a)
                self.logger.debug('recv {}: {}'.format(k, a))

            elif k == 'order':
                # FIXME: make a function to get strategy bot ID
                _id = int(str(a)[-3:])
                if _id in self.conn_sb:
                    self.conn_sb[_id].send((k, a),)

                else:
                    self.logger.error('Cannot compute PnL, no connection with '
                                      'ID {} StrategyBot'.format(_id))

            elif k is None:
                pass

            else:
                self.logger.error('unknown {}: {}'.format(k, a))

            if self.is_stop():
                self.conn_om.shutdown()

        self.logger.debug('end listen OrderManager')

    def listen_sb(self, _id):
        """ Update fees and balance when received them from OrdersManager. """
        _msg = 'SB ID {} - '.format(_id)
        self.logger.debug(_msg + 'start')
        conn = self.conn_sb[_id]
        for k, a in conn:
            if k in self._handler_sb.keys():
                self._handler_sb[k](a, _id)
                self.logger.debug(_msg + '{}: {}'.format(k, a))

            elif k is None:
                pass

            else:
                self.logger.error(_msg + 'unknown {}: {}'.format(k, a))

            if self.is_stop():
                conn.shutdown()

        self.logger.debug(_msg + 'end loop')

    def listen_cli(self):
        self.logger.debug('start listen CLI')
        for k, a in self.conn_cli:
            if self.is_stop():
                self.conn_cli.shutdown()

            if k is None:

                continue

            self.logger.info('CLI sent {} command'.format(k.upper()))
            if k == 'sb_update':
                self.logger.info('CLI sent UPDATE command')
                sb_update = {c: v.name for c, v in self.conn_sb.items()}
                self.conn_cli.send(('sb_update', sb_update),)

            elif k == '_stop':
                self.logger.info('CLI sent STOP command: {}'.format(a))
                for v in a:
                    if v == 'tradingbot':
                        self.set_stop(True)

                        break

                    elif v in [c.name for c in self.conn_sb.values()]:
                        _id = self.conn_sb.get_id(v)
                        self.conn_sb[_id].send(('_stop', None))

                    else:
                        self.logger.error('{} not in conn_sb'.format(v))

            elif k == 'start':
                self.logger.info('CLI sent START command: {}'.format(a))
                for v in a:
                    self.process_sb[v] = self.set_process(
                        start_strategy_bot, v, v,
                        address=self.address, authkey=self.authkey
                    )

            elif k == 'get_running_clients':
                running_clients = {
                    'orders_manager': str(self.conn_om.state),
                    'performance_manager': str(self.conn_tpm.state),
                    # 'strategy_bots': str(self.conn_sb),
                    'command_line_interface': str(self.conn_cli.state),
                    'strategy_bots': {
                        sb.name: sb.state for sb in self.conn_sb.values()
                    },
                }
                self.conn_cli.send(('running_clients', running_clients),)

            else:
                self.logger.error('Unknown command {}: {}'.format(k, a))

        self.logger.debug('end listen CLI')

    def client_manager(self):
        """ Listen client (OrderManager and StrategyManager). """
        self.logger.debug('start')
        p_om = None
        p_tpm = None
        while not self.is_stop():
            p_om = self.check_up_process(
                p_om, start_order_manager, 'OrdersManager', self.path_log,
                address=self.address, authkey=self.authkey
            )
            p_tpm = self.check_up_process(
                p_tpm, start_performance_manager, 'TradingPerformanceManager',
                address=self.address, authkey=self.authkey
            )

            if self.q_from_cli.empty():
                time.sleep(0.01)

                continue

            _id, action = self.q_from_cli.get()
            if action == 'up':
                self.setup_client(_id)

            elif action == 'down':
                self.shutdown_client(_id)

            else:
                self.logger.error('unknown action: {}'.format(action))

                raise ValueError('Unknown action: {}'.format(action))

        self.logger.debug('client_manager | stop')

    def set_process(self, target, name, *args, **kwargs):
        p = Process(target=target, name=name, args=args, kwargs=kwargs)
        self.logger.debug('Setup process: {}'.format(name))
        p.start()

        return p

    def check_up_process(self, p, target, name, *args, **kwargs):
        if self.auto and p is None:
            p = self.set_process(target, name, *args, **kwargs)

        elif self.auto and not p.is_alive():
            self.logger.debug('Process {} is not alive'.format(name))
            p.join()
            p = None

        return p

    def setup_client(self, _id):
        """ Set up a client thread (OrdersManager, StrategyBot, etc.).

        Parameters
        ----------
        _id : int
            ID of the client. If equal to 0 then setup an OrdersManager, else
            setup a StrategyBot.

        """
        self.logger.debug('Client ID {}'.format(_id))
        if _id == 0:
            # start thread listen OrderManager
            self.conn_om.thread = Thread(
                target=self.listen_om,
                daemon=True
            )
            self.conn_om.thread.start()

        elif _id == -1:
            # TradingPerformance started
            # not need to run a thread ?
            pass

        elif _id == -2:
            # start thread for CLI
            self.conn_cli.thread = Thread(
                target=self.listen_cli,
                daemon=True,
            )
            self.conn_cli.thread.start()

        else:
            # start thread listen StrategyBot
            self.conn_sb[_id].thread = Thread(
                target=self.listen_sb,
                kwargs={'_id': _id},
                daemon=True
            )
            self.conn_sb[_id].thread.start()

    def shutdown_client(self, _id):
        """ Shutdown a client thread (OrdersManager, StrategyBot, etc.).

        Parameters
        ----------
        _id : int
            ID of the client. If equal to 0 then setup an OrdersManager, else
            setup a StrategyBot.

        """
        if _id == 0:
            conn = self.conn_om

        elif _id == -1:
            # NOTHING NEEDED HERE ?
            pass

        elif _id == -2:
            conn = self.conn_cli

        else:
            conn = self.conn_sb.pop(_id)
            name = conn.name

        self.logger.debug('{}'.format(conn))

        # shutdown connection with client
        if conn.state == 'up':
            conn.shutdown()

        if conn.thread is not None:
            conn.thread.join()

        if _id > 0 and name in self.process_sb:
            self.logger.debug('wait to process {} join TBM'.format(name))
            p = self.process_sb.pop(name)
            p.join()

        self.logger.debug('shutdown Client ID {}'.format(_id))


def start_order_manager(path_log, exchange='kraken', address=('', 50000),
                        authkey=b'tradingbot'):
    """ Start order manager client. """
    from orders_manager import OrdersManager as OM

    om = OM(address=address, authkey=authkey)
    with om(exchange, path_log):
        om.loop()

    return None


def start_performance_manager(address=('', 50000), authkey=b'tradingbot'):
    """ Start trading performance manager client. """
    from performance import TradingPerformanceManager as TPM

    tpm = TPM(address=address, authkey=authkey)
    with tpm:
        tpm.loop()

    return None


def start_strategy_bot(strat_name, address=('', 50000), authkey=b'tradingbot'):
    """ Start a strategy bot. """
    from strategy_manager import StrategyBot as SB

    sb = SB(address=address, authkey=authkey)
    with sb(strat_name):
        for s, kw in sb:
            sb.process_signal(s, kw)

    return None


if __name__ == '__main__':

    import logging.config
    import yaml
    import sys

    with open('./trading_bot/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    if len(sys.argv) > 1 and 'auto' in sys.argv[1:]:
        auto = True

    else:
        auto = False

    tbm = TradingBotManager(auto=auto)
    with tbm:
        try:
            # tbm.runtime()
            t0 = int(time.time())
            for _ in tbm:
                txt = '{} | Have been started {} ago'.format(
                    time.strftime('%y-%m-%d %H:%M:%S'),
                    str_time(int(time.time() - t0)),
                )
                print(txt, end='\r')

        except KeyboardInterrupt:
            tbm.logger.error('Stop with KeyboardInterrupt')
            tbm.set_stop(True)
            time.sleep(1)
