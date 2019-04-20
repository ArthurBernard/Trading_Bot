# Strategy Manager - Manager for algorithmic financial strategies [In Progress]

## Description

This project is in progress, eventually it will be able to automatically manage several strategies, signal calculation, order execution, allow history performance, etc.    

Initially `Strategy Manager` will be used with the Kraken crypto-currency exchange platform, but in the long term this project may be extended to other trading platforms (e.g. Bitfinex or Interactive-Brokers).    

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

- General: 
     - Clean and standardize different loggers and log files;
     - Make a documentation;
- `bot_download_data.sh` shell to request data and verify that it runs fine.
- `bot_manager.sh` shell script to run and verify that all bots run fine. 
- `orders_manager.py`:
    - Method: Set history orders;
    - Method: Get available funds;
    - Method: Verify integrity of new orders (if volume to order is available); 
- `execution_order.py`: Algorithm of execution order (not just 'submit and leaves'). Something like vwap or twap. 
- `result_manager.py`:
    - Function: compute performance of a strategy
    - Object: special logger 