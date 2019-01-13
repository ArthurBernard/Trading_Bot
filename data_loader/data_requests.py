#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Import built-in packages
from pickle import Pickler, Unpickler
import time

# Import external packages
import requests
import json

"""
TODO list:
    - To create method: Differents kind of requests; 
    - To create method: Save data;
    - To finish method: Special iterative method;
    - To create method: sort and clean data;
"""

class DataRequests:
    """
    Description..

    Methods
    -------
    

    Attributes
    ----------

    """
    def __init__(self, timestep, request, save_path='../data_base/',
        API_path='https://api.kraken.com/0/public/', **request_params):
        """
        Parameters
        ----------
        :timestep: int
            Number of seconds between two data requests.
        :request: function ? str ? 
            Kind of data requests.
        :save_path: str
            Path of the folder to save data.
        :API_path: str
            Path of the public API.
        :request_params: Parameters to requests API.
        """
        self.timestep = timestep
        self.request = request
        self.save_path = save_path
        self.API_path = API_path
        self.request_params = request_params

    def __iter__(self):
        return self

    def __next__(self):
        """
        TODO: Finish this method

        request => sort and clean => save data => REPEAT
        """
        if self._condition():
            raise StopIteration
        self.sort_data(self.resquest_API(**self.request_params))
        self.save()
        return self

    def _condition(self):
        """
        TODO:

        Verify if script have to stop.

        Return
        ------
        Boolean.
        """
        pass


    def request_API(self, **kwargs):
        """
        Request method.

        Parameters
        ----------
        :kwargs: see parameters on the official API doc.

        Return
        ------
        :data: Output data request.
        """
        try:
            out = requests.get(self.path + self.request, kwargs)
            data = json.loads(out.text)['result']
        except Error:
            # TODO: see exeptions allowed
            pass
        return data
    
    def sort_data(self, data):
        """ 
        Transform raw data to clean data, ready to save in csv.
        
        Parameters
        ----------
        :data: dict
            Raw data.

        Return
        ------
        :clean_data: pd.DataFrame ? dict ? list ? see .csv for python
            Data cleaned.
        """
        pass

    def save(self):
        """
        Save data in the data base.
        database/underlying/date.csv
        """
        pass