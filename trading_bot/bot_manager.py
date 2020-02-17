#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 09:58:03
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-17 17:09:49

""" Set a server and run each bot. """

# Built-in packages
from threading import Thread
from multiprocessing import Process
import logging
import time
import os

# Third party packages

# Local packages
from trading_bot._server import _TradingBotManager
from trading_bot._server import TradingBotServer as TBS
from trading_bot.results_manager import update_order_hist, ResultManager
from trading_bot.strategy_manager import StrategyManager as SM
from trading_bot.tools.time_tools import str_time

__all__ = [
    'TradingBotManager', 'start_order_manager', 'start_tradingbotserver',
]


class TradingBotManager(_TradingBotManager):
    """ Trading Bot Manager object. """

    # TODO : load general config
    # TODO : run strategies

    def __init__(self, address=('', 50000), authkey=b'tradingbot', s=9):
        """ Initialize Trading Bot Manager object. """
        _TradingBotManager.__init__(self, address=address, authkey=authkey)

        self.logger = logging.getLogger(__name__ + '.TradingBotManager')
        self.logger.info('init | current PID is {}'.format(os.getpid()))
        self.t = time.time()
        self.path_log = '/home/arthur/Strategies/Data_Server/Untitled_Document2.txt'
        self.address = address
        self.authkey = authkey
        self.fees = {}
        self.balance = {}
        self.txt = {}

        # Set threads
        server_thread = Thread(
            target=self.set_server,
            kwargs={'address': address, 'authkey': authkey}
        )
        # bot_thread = Thread(target=self.runtime, args=(s,))
        listen_thread = Thread(target=self.listen_om, daemon=True)
        server_thread.start()
        listen_thread.start()
        # bot_thread.start()
        # server_thread.join()
        # bot_thread.join()
        # self.set_fees()
        self.logger.info('init | init finished')
        self.runtime(s)

    def set_server(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize a server connection. """
        self.m = TBS(address=address, authkey=authkey)
        self.s = self.m.get_server()
        self.logger.info('set_server | started')
        # print(self.stop)
        self.state['stop'] = False
        self.s.serve_forever()
        self.logger.info('set_server | stopped')

    def runtime(self, s=9):
        """ Do something. """
        # TODO : Run OrderManagerClient object
        # TODO : Run all StrategyManagerClient objects
        self.logger.debug('run | start to do something')
        # Start bot OrdersManager
        # p_om = Process(
        #    target=start_order_manager,
        #    name='truc',
        #    args=(self.path_log,),
        #    kwargs={'address': self.address, 'authkey': self.authkey}
        # )
        # p_om.start()
        while time.time() - self.t < s:
            # print('{:.1f} sec.'.format(time.time() - self.t), end='\r')
            txt = '{} | Have bean started {} ago'.format(
                time.strftime('%y-%m-%d %H:%M:%S'),
                str_time(int(time.time() - self.t)),
            )
            print(txt, end='\r')
            time.sleep(0.01)

        self.logger.debug('run | end to do something')
        self.state['stop'] = True
        self.logger.debug('run | stop propageted')

        # Wait until child process closed
        # p_om.join()

        time.sleep(3)
        self.s.stop_event.set()
        self.logger.info('run | TradingBotManager stopped.')

    def run_strategy(self, name):
        # /!\ DEPRECATED /!\
        # TODO : set a dedicated pipe
        # TODO : run a new process for a new strategy
        with SM(name, address=self.address, authkey=self.authkey) as sm:
            sm.set_configuration(self.path + name + '/configuration.yaml')
            for s, kw in sm:
                if s is not None:
                    self.logger.info(
                        'run_strat | Signal: {} | Params: {}'.format(s, kw)
                    )
                    output = sm.set_order(s, **kw, **sm.ord_kwrds)

                    if output:
                        self.logger.info(
                            'run_strat | executed order : {}'.format(output)
                        )

                self.txt[name] += '{} | Next signal in {}'.format(
                    name, str_time(int(sm.next - sm.TS))
                )

    def listen_om(self):
        """ Update fees and balance when received them from OrdersManager. """
        while True:
            msg = self.r_om.recv()
            for k, a in msg.items():
                if k == 'fees':
                    self.fees.update(a)
                    self.logger.info('listen_om | fees updated')

                elif k == 'balance':
                    self.balance.update(a)
                    self.logger.info('listen_om | balance updated')

                elif k == 'closed_order':
                    self.set_closed_order(a)
                    self.logger.info('listen_om |closed_order updated')

                else:
                    self.logger.error('listen_om| unknown {}: {}'.format(k, a))

    def set_closed_order(self, result):
        """ Update closed orders and send it to ResultManager.

        Parameters
        ----------
        result : dict
            {'txid': list, 'price': float, 'vol_exec': float, 'fee': float,
            'feeq': float, 'feeb': float, 'cost': float, 'start_time': int,
            'userref': int, 'type': str, 'volume', float, 'pair': str,
            'ordertype': str, 'level': int, 'end_time': int, 'fee_pct': float,
            'strat_id': int}.

        """
        self.logger.debug('set_closed_order | id {}'.format(result['userref']))
        update_order_hist(result, name='', path='./results/')
        # TODO : update vol to strategy_manager


def start_order_manager(path_log, exchange='kraken', address=('', 50000),
                        authkey=b'tradingbot'):
    """ Start order manager client. """
    from orders_manager import OrdersManager as OM

    om = OM(path_log, exchange=exchange, address=address, authkey=authkey)
    om.start_loop()

    return None


def start_tradingbotserver(address=('', 50000), authkey=b'tradingbot'):
    """ Set the trading bot server. """
    q_orders = Queue()
    TBS.register('get_queue_orders', callable=lambda: q_orders)
    # q2 = Queue()
    # TradingBotManager.register('get_queue2', callable=lambda: q2)
    m = TBS(address=address, authkey=authkey)
    s = m.get_server()
    s.serve_forever()


if __name__ == '__main__':

    import logging.config
    import yaml

    with open('./trading_bot/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    # start_tradingbotmanager()
    tbm = TradingBotManager(s=500)
    # tbm.run()
