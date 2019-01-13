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

## TODO 

By order of priority:

- `strategy_manager.py`:   
    - To finish method: Special callable method (set signal strategy);   
    - To create method: Iso-volatility (apply a coefficient following the volatility of underlying);   
    - To create method: Special iterative method (loop following a timestep);
    - To create method: Load data method (use LoaderData object);
    - To create method: other methods ?;
- `instruction.sh` shell script to verify that all scripts run fine.
- `data_requests.py`:
    - To create method: Differents kind of requests; 
    - To create method: Save data;
    - To finish method: Special iterative method;
    - To create method: sort and clean data;
- `data_request.py`: 
    - To create method: Load data;
    - To create method: Get ready data;
- `orders_manager.py`:
    - To finish method: Order;
    - To create method: Set history orders;
    - To create method: Get pending orders;
    - To create method: Get available funds;
    - To create method: Verify integrity of new orders;
    - To create method: (future) split orders for a better scalability;