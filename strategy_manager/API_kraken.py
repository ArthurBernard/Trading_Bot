#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import hashlib
import hmac
import time
import base64

# Import external packages
import requests

# Import internal packages


__all__ = ['KrakenClient']


class KrakenClient:
    """ Object to connect, request data and query orders to Kraken Client API.

    """

    def __init__(self, key=None, secret=None):
        """ Initialize parameters.

        Parameters
        ----------
        key : str, optional
            Key to connect to Kraken Client API.
        secret : str, optional
            Secret to connect to Kraken Client API.

        """
        self.uri = "https://api.kraken.com"
        self.key = key
        self.secret = secret

    def load_key(self, path):
        """ Load key and secret from a text file.

        Parameters
        ----------
        path : str
            Path of file with key and secret.

        """
        with open(path, 'r') as f:
            self.key = f.readline().strip()
            self.secret = f.readline().strip()

    def _nonce(self):
        """ Returns a nonce used in authentication.

        """
        return int(time.time() * 1000)

    def _headers(self, path, nonce, data):
        """ Set header with signature for authentication.

        """
        post_data = [str(key) + '=' + str(arg) for key, arg in data.items()]
        post_data = str(data['nonce']) + '&'.join(post_data)
        message = path.encode() + hashlib.sha256(post_data.encode()).digest()

        h = hmac.new(
            base64.b64decode(self.secret),
            message,
            hashlib.sha512
        )

        signature = base64.b64encode(h.digest()).decode()

        return {'API-Key': self.key, 'API-sign': signature}

    def query_private(self, method, timeout=30, **kwargs):
        """ Set a request.

        Parameters
        ----------
        method : str
            Kind of request.
        kwargs : dict, optional
            Parameters of the request, cf Kraken Client API.

        Returns
        -------
        dict
            Answere of Kraken Client API.

        """
        nonce = self._nonce()
        kwargs['nonce'] = nonce
        path = '/0/private/' + method
        headers = self._headers(path, nonce, kwargs.copy())
        url = self.uri + path

        r = requests.post(url, headers=headers, data=kwargs, timeout=timeout)

        if r.status_code in [200, 201, 202]:
            return r.json()
        else:
            raise ValueError(r.status_code, r)
