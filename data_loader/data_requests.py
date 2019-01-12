#!/usr/bin/env python
# -*- coding: utf-8 -*-

from pickle import Pickler, Unpickler
import time

"""
TODO list:
    - 
"""

class DataRequests:
    """
    Description..

    Methods
    -------
    todo: - differents kind of requests; 
          - save data;
          - iter method;
          - sort and clean data;

    Attributes
    ----------

    """
    def __init__(self, timestep, request):
        """
        Parameters
        ----------
        :timestep: int
            Number of seconds between two data requests.
        :request: function ? str ? 
            Kind of data requests.
        """
        self.timestep = timestep
        self.request = request
    
    def sort_data(self, data):
        """ 
        Transform raw data to clean data, ready to save in csv.
        
        Parameters
        ----------
        :data: dict ? json ? list ?
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