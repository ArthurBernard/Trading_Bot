#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import json
import hashlib
import hmac
import time
import base64

# Import external packages
import requests

# Import internal packages


__all__ = ['KrakenClient']


class KrakenClient:
    def __init__(self, key=None, secret=None):
        self.uri = "https://api.kraken.com/"
        self.key = key
        self.secret = secret

    def load_key(self, path):
        with open(path, 'r') as f:
            self.key = f.readline().strip()
            self.secret = f.readline().strip()

    def _nonce(self):
        """
        Returns a nonce
        Used in authentication
        """
        return int(time.time() * 1000)

    def _headers(self, path, data):
        message = '/' + path + json.dumps(data)
        h = hmac.new(
            base64.b64decode(self.secret),
            message.encode(),
            hashlib.sha512
        )

        signature = base64.b64encode(h.digest()).decode()

        return {
            'API-Key': self.key,
            'API-sign': signature,
        }

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
        path = '0/private/' + method
        headers = self._headers(path, kwargs)
        url = self.uri + path

        r = requests.post(url, headers=headers, data=kwargs, timeout=timeout)

        if r.status_code in [200, 201, 202]:
            return r.json()
        else:
            raise ValueError(r.status_code, r)
