#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 09:58:03
# @Last modified by: ArthurBernard
# @Last modified time: 2020-03-06 08:41:08

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

    def __init__(self, address=('', 50000), authkey=b'tradingbot', auto=False):
        """ Initialize Trading Bot Manager object. """
        _TradingBotManager.__init__(self, address=address, authkey=authkey)

        self.logger = logging.getLogger(__name__)
        self.t = int(time.time())
        self.path_log = ('/home/arthur/Strategies/Data_Server/'
                         'Untitled_Document2.txt')
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

    def __repr__(self):
        return 'order bot is {} | {} are running'.format(
            self.order_bot, self.strat_bot
        )

    def runtime(self, s=9):
        """ Do something. """
        # TODO : Run OrderManagerClient object
        # TODO : Run all StrategyManagerClient objects
        self.logger.debug('start runtime loop')
        while time.time() - self.t < s:
            # print('{:.1f} sec.'.format(time.time() - self.t), end='\r')
            txt = '{} | Have been started {} ago'.format(
                time.strftime('%y-%m-%d %H:%M:%S'),
                str_time(int(time.time() - self.t)),
            )
            txt += ' | Stop in {} seconds'.format(
                str_time(self.t + s - int(time.time()))
            )
            print(txt, end='\r')
            time.sleep(0.01)

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
        self.logger.debug('start')
        for k, a in self.conn_om:
            if k in self._handler_om.keys():
                self._handler_om[k](a)
                self.logger.debug('{}: {}'.format(k, a))

            elif k == 'fees':
                self.state['fees'].update(a)
                self.logger.debug('recv {}: {}'.format(k, a))

            elif k == 'balance':
                self.state['balance'].update(a)
                self.logger.debug('recv {}: {}'.format(k, a))

            elif k is None:
                pass

            else:
                self.logger.error('unknown {}: {}'.format(k, a))

            if self.is_stop():
                self.conn_om.shutdown()

        self.logger.debug('end')

    def listen_sb(self, _id):
        """ Update fees and balance when received them from OrdersManager. """
        _msg = 'SB ID {} - '.format(_id)
        self.logger.debug(_msg + 'start')
        conn = self.conn_sb[_id]
        for k, a in conn:
            if k in self._handler_sb.keys():
                self._handler_sb[k](a, _id)
                self.logger.debug(_msg + '{}: {}'.format(k, a))
                # if _id != conn.id:
                #    _id = conn.id
                #    _msg = 'listen_sb {} | '.format(_id)

            elif k is None:
                pass

            else:
                self.logger.error(_msg + 'unknown {}: {}'.format(k, a))

            if self.is_stop():
                conn.shutdown()

        self.logger.debug(_msg + 'end loop')

    def client_manager(self):
        """ Listen client (OrderManager and StrategyManager). """
        self.logger.debug('start')
        t = time.time()
        p_om = None
        while not self.is_stop():
            if time.time() - t > 0:
                self.logger.debug('StrategyBot: {}'.format(self.conn_sb))
                self.logger.debug('OrdersManager: {}'.format(self.conn_om))
                t += 900

            if self.auto and p_om is None:
                # Start bot OrdersManager
                self.logger.debug('Setup process OrdersManager:')
                p_om = Process(
                    target=start_order_manager,
                    name='truc',
                    args=(self.path_log,),
                    kwargs={'address': self.address, 'authkey': self.authkey}
                )
                p_om.start()

            elif self.auto and not p_om.is_alive():
                self.logger.debug('Process OrdersManager is not alive')
                p_om.join()
                p_om = None

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

    def setup_client(self, _id):
        """ Set up a client thread (OrdersManager, StrategyBot, etc.).

        Parameters
        ----------
        _id : int
            ID of the client. If equal to 0 then setup an OrdersManager, else
            setup a StrategyBot.

        """
        self.logger.debug('Client ID-{}'.format(_id))
        if _id == 0:
            # start thread listen OrderManager
            self.conn_om.thread = Thread(
                target=self.listen_om,
                daemon=True
            )
            self.conn_om.thread.start()

        elif _id == -1:
            # TradingPerformance started
            pass

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

        else:
            conn = self.conn_sb.pop(_id)

        self.logger.debug('{}'.format(conn))

        # shutdown connection with client
        if conn.state == 'up':
            conn.shutdown()

        conn.thread.join()
        self.logger.debug('shutdown Client ID {}'.format(_id))


def start_order_manager(path_log, exchange='kraken', address=('', 50000),
                        authkey=b'tradingbot'):
    """ Start order manager client. """
    from orders_manager import OrdersManager as OM

    om = OM(address=address, authkey=authkey)
    with om(exchange, path_log):
        om.loop()

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

    if len(sys.argv) > 1 and isinstance(sys.argv[1], int):
        s = sys.argv[1]

    elif len(sys.argv) > 2 and isinstance(sys.argv[2], int):
        s = sys.argv[2]

    else:
        s = 1e8

    tbm = TradingBotManager(auto=auto)
    with tbm:
        tbm.runtime(s=s)
