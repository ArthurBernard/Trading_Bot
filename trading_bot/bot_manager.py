#!/usr/bin/env python3
# coding: utf-8
# @Author: arthur
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-01-27 09:58:03
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-27 10:55:32

""" Set a server and run each bot. """

# Built-in packages
import logging
from threading import Thread
import time

# Third party packages

# Local packages
from trading_bot._server import _TradingBotManager, TradingBotServer as TBS
from trading_bot.strategy_manager import StrategyBot as SB
from trading_bot.tools.time_tools import str_time

__all__ = [
    'TradingBotManager', 'start_order_manager', 'start_tradingbotserver',
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

    def __init__(self, address=('', 50000), authkey=b'tradingbot'):
        """ Initialize Trading Bot Manager object. """
        _TradingBotManager.__init__(self, address=address, authkey=authkey)

        self.logger = logging.getLogger(__name__)
        self.t = time.time()
        self.path_log = '~/Strategies/Data_Server/Untitled_Document2.txt'
        self.address = address
        self.authkey = authkey
        self.txt = {}
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
        self.logger.debug('start')
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
            txt = '{} | Have been started {} ago'.format(
                time.strftime('%y-%m-%d %H:%M:%S'),
                str_time(int(time.time() - self.t)),
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

            elif k == 'closed_order':
                self.set_closed_order(a)

            elif k == 'order':
                self.logger.info('recv: {}'.format(a))
                _id = _get_id_strat(a.id)
                self.conn_sb[_id].send((k, a),)

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
        while not self.is_stop():
            if time.time() - t > 0:
                self.logger.debug('StrategyBot: {}'.format(self.conn_sb))
                self.logger.debug('OrdersManager: {}'.format(self.conn_om))
                t += 30

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
        """ Setup a client thread (OrdersManager, StrategyBot, etc.).

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

    def set_closed_order(self, result):
        """ Update closed orders and send it to StrategyBot.

        Parameters
        ----------
        result : dict
            {'txid': list, 'price': float, 'vol_exec': float, 'fee': float,
            'feeq': float, 'feeb': float, 'cost': float, 'start_time': int,
            'userref': int, 'type': str, 'volume', float, 'pair': str,
            'ordertype': str, 'level': int, 'end_time': int, 'fee_pct': float,
            'strat_id': int}.

        """
        conn = self.conn_sb[result['strat_id']]
        self.logger.debug('Order ID {}'.format(result['userref']))
        update_order_hist(result, name=conn.name + '/', path='./strategies/')
        conn.send(result)
        # TODO : send it also to result manager ?


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


def _get_id_strat(id_order, n=3):
    return int(str(id_order)[-n:])


if __name__ == '__main__':

    import logging.config
    import yaml

    with open('./trading_bot/logging.ini', 'rb') as f:
        config = yaml.safe_load(f.read())

    logging.config.dictConfig(config)

    # start_tradingbotmanager()
    tbm = TradingBotManager()
    with tbm:
        tbm.runtime(s=2000)
    # tbm.run()
