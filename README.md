# Strategy Manager - Manager for algorithmic financial strategies [In Progress]

## Description

This project is in progress, eventually it will be able to automatically manage several strategies, signal calculation, order execution, allow history performance, etc.    

Initially `Strategy Manager` can be used with the Kraken crypto-currency platform, but in the long term this project will be extended to other trading platforms (e.g. Bitfinex or Interactive-Brokers).    

## Installation

Clone the repository and at the root of the folder:

> $ pip install strategy_manager   

With linux you can use crontab to schedule automatic exectution.   

Exemple:   
> $ crontab -e   
>> 0 0 * * * python running_strat_manager.py   
