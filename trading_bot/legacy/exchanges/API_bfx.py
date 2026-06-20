#!/usr/bin/env python3
# coding: utf-8

# Import built-in packages
import json
import hashlib
import hmac
import time

# Import external packages
import requests

# Import internal packages


__all__ = ['BitfinexClient']


class BitfinexClient:
    def __init__(self):
        self.uri = "https://api.bitfinex.com/"

    def load(self, path):
        with open(path, 'r') as f:
            self.key = f.readline().strip()
            self.secret = f.readline().strip()

    def _nonce(self):
        """
        Returns a nonce
        Used in authentication
        """
        return str(int(time.time() * 1000))

    def _headers(self, path, nonce, body):
        signature = "/api/" + path + nonce + body
        h = hmac.new(
            self.secret.encode('utf8'),
            signature.encode('utf8'),
            hashlib.sha384
        )
        signature = h.hexdigest()

        return {
            "bfx-nonce": nonce,
            "bfx-apikey": self.key,
            "bfx-signature": signature,
            "content-type": "application/json"
        }

    def set_request(self, method, **kwargs):
        """ Set a request.

        Parameters
        ----------
        method : str
            Kind of request.
        kwargs : dict, optional
            Parameters of the request, cf Bitfinex Client API.

        Returns
        -------
        dict
            Answere of Bitfinex Client API.

        """
        nonce = self._nonce()
        body = json.dumps(kwargs)
        path = 'v2/auth/r/' + method
        headers = self._headers(path, nonce, body)

        r = requests.post(self.uri + path, headers=headers, data=body)

        if r.status_code == 200:
            return r.json()
        else:
            raise ValueError(r.status_code, r)
