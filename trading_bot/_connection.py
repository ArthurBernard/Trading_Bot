#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-20 16:35:31
# @Last modified by: ArthurBernard
# @Last modified time: 2020-03-31 18:20:21

""" Objects to send and receive objetcs between clients. """

# Built-in packages
import logging
import time

# Third party packages

# Local packages

__all__ = [
    'ConnStrategyBot', 'ConnOrderManager', 'ConnTradingBotManager',
    'ConnPerformanceManager',
]


class _BasisConnection:
    state = None
    r = None
    w = None
    thread = None

    def __init__(self, _id, name='connection'):
        # self.logger = logging.getLogger(__name__)
        self.logger = logging.getLogger(__name__ + '.{}-{}'.format(name, _id))
        self.id = _id
        self.name = name

    def __iter__(self):
        return self

    def __next__(self):
        if self.state == 'down':
            self._shutdown()

            raise StopIteration

        if self.poll():

            return self._handler(*self.recv())

        else:
            time.sleep(0.1)

            return None, None

    def __repr__(self):
        return 'Conn ID {self.id:2} - with {self.name} is {self.state}'.format(
            self=self
        )

    def setup(self, reader, writer):
        self.state = 'up'
        self.r = reader
        self.w = writer
        self.logger.debug('setup {}'.format(self))

    def shutdown(self, msg=None):
        self.state = 'down'
        if msg is not None:
            self.logger.debug('shutdown because {}'.format(msg))

        self.logger.debug('shutdown {}'.format(self))
        self._shutdown()

    def recv(self):
        k, a = self.r.recv()

        return k, a

    def send(self, msg):
        self.w.send(msg)

    def poll(self):
        return self.r.poll()

    def _handler(self, k, a):
        if k == 'stop':
            self.shutdown(msg=a)

            raise StopIteration

        return k, a

    def _set_reader(self, reader):
        self.r = reader
        if self.w is not None:
            self.state = 'up'

        self.logger.debug('set reader {}'.format(self))

    def _set_writer(self, writer):
        self.w = writer
        if self.r is not None:
            self.state = 'up'

        self.logger.debug('set writer {}'.format(self))

    def _shutdown(self):
        self.r.close()
        self.w.close()


class ConnCLI(_BasisConnection):
    """ Connection with Command Line Interface. """

    def __init__(self):
        super(ConnCLI, self).__init__(-2, name='CLI')


class ConnOrderManager(_BasisConnection):
    """ Connection object to OrderManager object. """

    def __init__(self):
        super(ConnOrderManager, self).__init__(0, name='order_manager')


class ConnPerformanceManager(_BasisConnection):
    """ Connection object to TradingPerformanceManager object. """

    def __init__(self):
        super(ConnPerformanceManager, self).__init__(-1, name='perf_manager')


class ConnStrategyBot(_BasisConnection):
    """ Connection object to StrategyBot object. """

    def __init__(self, _id, name='StratBot'):
        super(ConnStrategyBot, self).__init__(_id, name)

    def _handler(self, k, a):
        k, a = super(ConnStrategyBot, self)._handler(k, a)
        if k == 'name':
            self.name = a

        else:

            return k, a

        return None, None


class ConnTradingBotManager(_BasisConnection):
    """ Connection object to TradingBotManager object. """

    def __init__(self, _id):
        super(ConnTradingBotManager, self).__init__(_id, name='TBM')
