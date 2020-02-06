#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-02-06 11:57:48
# @Last modified by: ArthurBernard
# @Last modified time: 2020-02-06 20:39:02

# Built-in packages
import time

# Third party packages

# Local packages


class Order:
    """ Order object. """

    def __init__(self, id, exchange_client, status=None, input={}):
        """ Initialize an order object.

        Parameters
        ----------
        id : int
            ID of the order (32-bit signed integer).
        exchange_client : ExchangeClient
            Object to connect with the client API of the exchange.
        status : {'open', 'closed', 'pending', 'canceled'}
            State of the order. Default is None.
        input : dict (optional)
            Input to request order.

        """
        self.start = int(time.time())
        self.id = id
        self.exchange_client = exchange_client
        self.status = status
        self.input = input
        self.volume = input['volume']
        self.output = {'closed': {}, 'open': {}}
        self.answere = {}

    def set_answere(self, ans):
        """ Set output answere. """
        self.answere[ans['txid']] = ans['descr']

    def __setitem__(self, key, value):
        """ Set output item. """
        self.output[key].update(value)

    def update(self, output):
        """ Update output item. """
        for k, v in output.items():
            self.output[k].update(v)

    def execute(self):
        """ Execute the order. """
        if self.status is None:
            ans = self._request('AddOrder', userref=self.id, **self.input)
            self.set_answere(ans)
            self.status = 'open'

        else:
            raise StatusError

    def get_open(self):
        """ Get open orders for this ID. """
        ans = self._request('OpenOrders', userref=self.id)
        if not ans['open']:
            self.set_closed()

        self.update(ans)

    def get_closed(self):
        """ Get closed orders for this ID. """
        ans = self._request('ClosedOrders', userref=self.id, start=self.start)
        self.update(ans)

    def cancel(self):
        """ Cancel the order. """
        ans = self._request('CancelOrder', txid=self.id)
        # TODO : finish

    def _request(self, method, **kwargs):
        return self.exchange_client.query_private(method, **kwargs)

    def set_closed(self):
        """ Close the order. """
        self.get_closed()
        for k, v in self.output['closed'].items():
            self.volume -= v['vol']

        if self.volume == 0.:
            self.status = 'closed'

        else:
            raise ErrorVolume(self)

    def __repr__(self):
        """ Represent the order. """
        txt = ("{self.id}: 'status': {self.status}, 'ansewere': {self.answere}"
               ", 'output': {self.output}".format(self=self))

        return txt


class OrderCollection(dict):
    """ Order collection object. """

    def __init__(self, *orders):
        """ Initialize a collection of order objects. """
        for o in orders:
            if not isinstance(o, Order):
                raise TypeError("{} must be an Order object".format(o))
        super(OrderCollection, self).__init__(**{o.id: o for o in orders})
        self._set_state()

    def __setitem__(self, key, value):
        print('set {}'.format(key))
        if not isinstance(value, Order):
            raise TypeError("{} must be an Order object".format(value))
        dict.__setitem__(self, key, value)
        self._add_state(key, value)

    def __delitem__(self, key):
        print('del {}'.format(key))
        dict.__delitem__(self, key)
        self._del_state(key)

    def __repr__(self):
        txt = 'waiting: {}, open: {}, close: {}'.format(
            self._waiting, self._open, self._close
        )
        for k, v in self.items():
            txt += '\nOrder {}: {}'.format(k, v)

        return txt

    def __eq__(self, other):
        if not isinstance(other, _Orders):

            return False

        return (other._waiting == self._waiting and
                other._open == self._open and
                other._close == self._close and
                dict.__eq__(self, other))

    def pop(self, key):
        print('pop {}'.format(key))
        self._del_state(key)

        return dict.pop(self, key)

    def popitem(self):
        key, value = dict.popitem(self)
        self._del_state(key)

        return key, value

    def update(self, *args, **kwargs):
        dict.update(self, *args, **kwargs)
        self._reset_state()

    def get_first(self):
        ordered_list = self.get_ordered_list()

        return ordered_list[0]

    def get_ordered_list(self):
        return self._waiting + self._open + self._close

    def pop_first(self):
        key = self.get_first()

        return key, self.pop(key)

    def _set_state(self):
        for key, value in self.items():
            self._add_state(key, value)

    def _reset_state(self):
        self._waiting, self._open, self._close = [], [], []
        self._set_state()

    def _add_state(self, key, value):
        if value.get('state') is None:
            self._waiting.append(key)

        elif value['state'] == 'open':
            self._open.append(key)

        elif value['state'] == 'close':
            self._close.append(key)

        else:
            raise ValueError('unknown state {}'.format(value['state']))

    def _del_state(self, key):
        if key in self._waiting:
            self._waiting.remove(key)

        elif key in self._open:
            self._open.remove(key)

        elif key in self._close:
            self._close.remove(key)

        else:
            raise ValueError('unknown id_order: {}'.format(key))
