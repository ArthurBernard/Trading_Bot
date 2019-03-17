# Strategy Manager - Manager for algorithmic financial strategies [In Progress]

## Description

This project is in progress, eventually it will be able to automatically manage several strategies, signal calculation, order execution, allow history performance, etc.    

Initially `Strategy Manager` can be used with the Kraken crypto-currency platform, but in the long term this project will be extended to other trading platforms (e.g. Bitfinex or Interactive-Brokers).    

## Installation

At the root of a folder, clone the repository and install it with `pip`:

> $ git clone https://github.com/ArthurBernard/Strategy_Manager.git
> $ pip install -e strategy_manager   

With linux you can use crontab to schedule automatic exectution.   

Exemple:   
> $ crontab -e   
>> 0 0 * * * python3 /path/Strategies_Manager/strategies_manager/main.py   

## TODO 

By order of priority:

- `strategy_manager.py`:   
    - Unit tests.
- `bot_download_data.sh` shell to request data and verify that it runs fine.
- `bot_strategies.sh` shell script to run and verify that all strategies run fine.
- `bot_manager.sh` shell script to run and verify that all bots run fine. 
- `data_requests.py`:
    - To create method: Differents kind of requests; 
    - To create method: Save data;
    - To finish method: Special iterative method;
    - To create method: sort and clean data;
- `data_request.py`: 
    - To create method: Load data;
    - To create method: Get ready data;
- `orders_manager.py`:
    - To create method: Set history orders;
    - To create method: Get available funds;
    - To create method: Verify integrity of new orders;
    - To create method: (future) split orders for a better scalability;