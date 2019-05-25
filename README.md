# Strategy Manager - Manager for financial algorithmic strategies [In Progress]

## /!\ Not yet working but comming soon ! /!\

## Description

This project is in progress, eventually it will be able to automatically manage several strategies, signal calculation, order execution, allow history performance, etc.    

Initially `Strategy Manager` will be used with the Kraken crypto-currency exchange platform, but in the long term this project may be extended to other trading platforms (e.g. Bitfinex, Bitmex or some more classical trading platforms as Interactive-Brokers). 

## Requirements

- System:
    - Unix OS (Linux or MacOS)

- Python version:
    - 3.5
    - 3.6
    - 3.7

- Python package:
    - requests
    - numpy
    - pandas
    - fynance

## Installation

At the root of a folder, clone the repository and install it with `pip`:

> $ git clone https://github.com/ArthurBernard/Strategy_Manager.git    
> $ pip install -e strategy_manager   

## Quick-start

1. Create a folder `./strategies/YOUR_STRATEGY_NAME`, with 3 scripts to configurate a strategy.

Example:

> $ mkdir ./strategies/YOUR_STRATEGY_NAME   
> $ touch ./strategies/YOUR_STRATEGY_NAME/\_\_init\_\_.py   
> $ touch./strategies/YOUR_STRATEGY_NAME/configuration.yaml   
> $ touch ./strategies/YOUR_STRATEGY_NAME/strategy.py   

TODO : explain how write `configuration.yaml` and `strategy.py`.

2. Append your strategy at the file `strategy_list_to_run.txt`.

Example:

> $ echo YOUR_STRATEGY_NAME >> ./execution_scripts/strategy_list_to_run.txt

3. With crontab you can schedule automatic execution every day. 

Example:

> $ crontab -e   
>> 0 0 * * * python3 /path/Strategies_Manager/execution_scripts/bot_manager.sh >> /path/Strategies_Manager/execution_scripts/manager.log 2>&1 &

## TODO list

By order of priority:

- General: 
     - Make a documentation;
- `bot_download_data.sh` shell to request data and verify that it runs fine.
- `bot_manager.sh` shell script to run and verify that all bots run fine. 
- `orders_manager.py`:
    - Method: Get available funds;
    - Method: Verify integrity of new orders (if volume to order is available); 
- `execution_order.py`: Algorithm of execution order (not just 'submit and leaves'). Something like vwap or twap. 
- `result_manager.py`:
    - Object: special logger 