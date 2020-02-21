#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-20 16:35:31
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-21 17:52:14

# Built-in packages
import logging
import time

# Third party packages

# Local packages

__all__ = ['Connection', 'ConnDict']


class Connection:
    state = None
    r = None
    w = None
    thread = None

    def __init__(self, _id, name='connection'):
        self.logger = logging.getLogger(__name__)
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
        return 'ID-{self.id:2} | {self.name} is {self.state}'.format(self=self)

    def setup(self, reader, writer):
        self.state = 'up'
        self.r = reader
        self.w = writer
        self.logger.debug('setup | {}'.format(self))

    def shutdown(self, msg=None):
        self.state = 'down'
        if msg is not None:
            self.logger.debug('shutdown | {}'.format(msg))

        self.logger.debug('shutdown | {}'.format(self))
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

    def _set_writer(self, writer):
        self.w = writer
        if self.r is not None:
            self.state = 'up'

    def _shutdown(self):
        self.r.close()
        self.w.close()


class ConnectionOrderManager(Connection):
    """ Connection object to OrderManager object. """

    def __init__(self):
        super(ConnectionOrderManager, self).__init__(0, name='order_manager')

    # def shutdown(self, msg=None):
    #    self.send(('stop', msg),)
    #    super(ConnectionOrderManager, self)._shutdown(msg=msg)


class ConnectionStrategyBot(Connection):
    """ Connection object to StrategyBot object. """

    def _handler(self, k, a):
        k, a = super(ConnectionStrategyBot, self)._handler(k, a)
        if k == 'name':
            self.name = a

        # elif k == 'switch_id':
        #    self.id = a

        #    return k, a

        else:

            return k, a

        return None, None


class ConnectionTradingBotManager(Connection):
    """ Connection object to TradingBotManager object. """

    def __init__(self, _id):
        super(ConnectionTradingBotManager, self).__init__(_id, name='TBM')

    # def _set_id(self, _id):
    #    self.id = _id
    #    self.send(('switch_id', _id),)


class ConnDict(dict):
    """ Connection collection object.

    Methods
    -------
    append
    # switch_id
    update

    """

    def __init__(self, *conn, **kwconn):
        """ Initialize a collection of connection objects. """
        self.logger = logging.getLogger(__name__ + '.ConnDict')
        for k, c in kwconn.items():
            self._is_conn(c)

        for c in conn:
            if self._is_conn(c):
                kwconn[str(c.id)] = c

        super(ConnDict, self).__init__(**kwconn)

    def __setitem__(self, _id, conn):
        """ Set item Connection object.

        Parameters
        ----------
        _id : int
            ID of the Connection object.
        conn : Connection
            The Connection object to append collect.

        """
        self.logger.debug('set | {}'.format(conn))
        self._is_conn(conn)
        dict.__setitem__(self, _id, conn)

    def __repr__(self):
        """ Represent the collection of connections.

        Returns
        -------
        str
            Representation of the collection of connections.

        """
        txt = ',\n'.join([str(c) for c in self.values()])

        return '{' + txt + '}'

    def __eq__(self, other):
        """ Compare self with other object.

        Returns
        -------
        bool
            True if self is equal to other, False otherwise.

        """
        if not isinstance(other, ConnDict):

            return False

        return dict.__eq__(self, other)

    def append(self, conn):
        """ Apend a Connection object to the collection.

        Parameters
        ----------
        conn : Connection
            Connection object to append.

        """
        self._is_conn(conn)
        if conn.id not in self.keys():
            self[conn.id] = conn

        else:
            self.logger.error('append | {} is already stored'.format(conn))

            raise ValueError('{} and {}'.format(conn, self[conn.id]))

    # def switch_id(self, new_id, ex_id):
    #    """ Remove `ex_id` ID and append `new_id` ID of a connection.
    #
    #    Parameters
    #    ----------
    #    new_id : int
    #        New ID of the connection object.
    #    ex_id : int
    #        Old ID of the connection to remove.
    #
    #    """
    #    self.logger.debug('switch_id | {} to {}'.format(ex_id, new_id))
    #    if new_id not in self.keys():
    #        self[new_id] = self.pop(ex_id)

    #    else:
    #        txt_err = 'ID-{} is already stored'.format(new_id)
    #        self.logger.error(txt_err)
    #        self[ex_id].send(('stop', txt_err),)

    def update(self, *conn, **kwconn):
        """ Update self with conn objects or an other collection of conn.

        Parameters
        ----------
        *conn : Connection or ConnDict
            Connection objects or collection of conn to update.
        **kwconn : Connection
            Connection objects to update.

        """
        for k, c in kwconn.items():
            self._is_conn(c)

        for c in conn:
            if isinstance(c, ConnDict):
                kwconn.update({k: v for k, v in c.items()})

            elif self._is_conn(c):
                kwconn[str(c.id)] = c

        dict.update(self, **kwconn)

    def _is_conn(self, obj):
        if not isinstance(obj, Connection):

            raise TypeError("{} must be a Connection object".format(obj))

        return True
