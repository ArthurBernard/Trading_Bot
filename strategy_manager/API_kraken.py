#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-05-06 20:53:46
# @Last modified by: ArthurBernard
# @Last modified time: 2020-01-24 16:56:29

""" Kraken Client API object. """

# Built-in packages
import hashlib
import hmac
import time
import base64
import logging
from json.decoder import JSONDecodeError

# External packages
import requests
from requests import HTTPError
from requests import ReadTimeout

# Internal packages


__all__ = ['KrakenClient']


class KrakenClient:
    """ Object to connect, request data and query orders to Kraken Client API.

    Attributes
    ----------
    key, secret : str
        Key and secret of Kraken Client API.
    path_log : str
        Path to read key and secret of Kraken Client API.

    Methods
    -------
    load_key
    query_prive

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
        self.logger = logging.getLogger('strat_man.' + __name__)

    def load_key(self, path):
        """ Load key and secret from a text file.

        Parameters
        ----------
        path : str
            Path of file with key and secret.

        """
        self.path_log = path
        with open(path, 'r') as f:
            self.key = f.readline().strip()
            self.secret = f.readline().strip()

    def _nonce(self):
        """ Return a nonce used in authentication. """
        return int(time.time() * 1000)

    def set_sign(self, path, data):
        """ Set signature for authentication. """
        post_data = [str(key) + "=" + str(arg) for key, arg in data.items()]
        post_data = str(data["nonce"]) + "&".join(post_data)
        # self.logger.debug("POST DATA: " + post_data)
        message = path.encode() + hashlib.sha256(post_data.encode()).digest()

        h = hmac.new(
            base64.b64decode(self.secret),
            message,
            hashlib.sha512
        )

        return base64.b64encode(h.digest())  # .decode()

    def query_private(self, method, timeout=30, **data):
        """ Set a request.

        Parameters
        ----------
        method : str
            Kind of request.
        data : dict, optional
            Parameters of the request, cf Kraken Client API.

        Returns
        -------
        dict
            Answere of Kraken Client API.

        """
        data['nonce'] = self._nonce()
        path = '/0/private/' + method
        headers = {'API-Key': self.key, 'API-sign': self.set_sign(path, data)}
        url = self.uri + path

        try:
            r = requests.post(url, headers=headers, data=data, timeout=timeout)
            if r.json()['error']:

                self.logger.error('ANSWERE: ' + str(r.json()))
                self.logger.error('URL: ' + str(url))
                self.logger.error('HEAD: ' + str(headers))
                self.logger.error('DATA: ' + str(data))

                raise ValueError(r.json()['error'], r.json())

            elif r.status_code in [200, 201, 202]:

                return r.json()['result']

            else:

                raise ValueError(r.status_code, r)

        except KeyError as e:
            error_msg = 'KeyError {} | '.format(type(e))
            error_msg += 'Request answere: {}'.format(r.json())
            error_msg += ' | Reload key/secret and retry request.'
            self.logger.error(error_msg, exc_info=True)
            self.load_key(self.path_log)
            time.sleep(5)

            return self.query_private(method, timeout=30, **data)
            # raise e

        except (NameError, JSONDecodeError) as e:
            error_msg = 'Output error: {} | Retry request.'.format(type(e))
            self.logger.error(error_msg)
            time.sleep(5)

            return self.query_private(method, timeout=30, **data)

        except (HTTPError, ReadTimeout) as e:
            error_msg = 'Connection error: {} | Retry request.'.format(type(e))
            self.logger.error(error_msg)
            time.sleep(5)

            return self.query_private(method, timeout=30, **data)

        except Exception as e:
            error_msg = 'Unknown error: {}\n'.format(type(e))
            error_msg += 'Request answere: {}'.format(r.json())
            self.logger.error(error_msg, exc_info=True)

            raise e
