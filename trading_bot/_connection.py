#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-20 16:35:31
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-20 16:38:30

# Built-in packages
import logging

# Third party packages

# Local packages

__all__ = ['Connection', 'ConnDict']


class Connection:
    state = None
    r = None
    w = None
    thread = None

    def __init__(self, _id, name='connection'):
        self.id = _id
        self.name = name

    def __repr__(self):
        return 'ID-{self.id:9} | {self.name} is {self.state}'.format(self=self)

    def setup(self, reader, writer):
        self.state = 'up'
        self.r = reader
        self.w = writer

    def shutdown(self, msg=None):
        if self.thread is not None:
            self.thread.join()

        self.send({'stop': msg})
        self.state = 'down'
        self.r.close()
        self.w.close()

    def recv(self):
        return self.r.recv()

    def send(self, msg):
        self.w.send(msg)

    def poll(self):
        return self.r.poll()

    def _set_reader(self, reader):
        self.r = reader
        if self.w is not None:
            self.state = 'up'

    def _set_writer(self, writer):
        self.w = writer
        if self.r is not None:
            self.state = 'up'


class ConnDict(dict):
    """ Connection collection object.

    Methods
    -------
    append
    update

    """

    def __init__(self, *conn, **kwconn):
        """ Initialize a collection of connection objects. """
        self.logger = logging.getLogger(__name__ + '.ConnDict')
        for k, v in kwconn.items():
            self._is_conn(v)

        for o in conn:
            if self._is_conn(o):
                kwconn[str(o.id)] = o

        super(ConnDict, self).__init__(**kwconn)

    def __setitem__(self, key, value):
        """ Set item conn.

        Parameters
        ----------
        key : int
            ID of the conn.
        value : Connection
            The conn object to collect.

        """
        self.logger.debug('set {}'.format(key))
        self._is_conn(value)
        dict.__setitem__(self, key, value)

    def __repr__(self):
        """ Represent the collection of connections.

        Returns
        -------
        str
            Representation of the collection of connections.

        """
        txt = ''
        for v in self.values():
            txt += '{}\n'.format(v)

        return txt

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
            conn object to append.

        """
        self._is_conn(conn)
        self[conn.id] = conn

    def update(self, *conn, **kwconn):
        """ Update self with conn objects or an other collection of conn.

        Parameters
        ----------
        *conn : Connection or ConnDict
            conn objects or collection of conn to update.
        **kwconn : Connection
            conn objects to update.

        """
        for k, v in kwconn.items():
            self._is_conn(v)

        for o in conn:
            if isinstance(o, ConnDict):
                kwconn.update({k: v for k, v in o.items()})

            elif self._is_conn(o):
                kwconn[str(o.id)] = o

        dict.update(self, **kwconn)

    def _is_conn(self, obj):
        if not isinstance(obj, Connection):

            raise TypeError("{} must be a Connection object".format(obj))

        return True
