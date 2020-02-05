# Trading_Bot - Autonomous Trading Bot in Python for algorithmic financial strategies [In Progress]

![GitHub](https://img.shields.io/github/license/ArthurBernard/Trading_Bot)
[![Language grade: Python](https://img.shields.io/lgtm/grade/python/g/ArthurBernard/Trading_Bot.svg?logo=lgtm&logoWidth=18)](https://lgtm.com/projects/g/ArthurBernard/Trading_Bot/context:python)

## /!\ Not yet working but comming soon ! /!\

## Description

This project is in progress, eventually it will be able to automatically manage several strategies, signal calculation, order execution, allow history performance, etc.    

Initially `Trading_Bot` will be used with the Kraken crypto-currency exchange platform, but in the long term this project may be extended to other trading platforms (e.g. Bitfinex, Bitmex or some more classical trading platforms as Interactive-Brokers). 

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

```bash
$ git clone https://github.com/ArthurBernard/Trading_Bot.git    
$ cd Trading_Bot    
$ pip install -e strategy_manager   
```

## Quick-start

1. Create a folder `./strategies/YOUR_STRATEGY_NAME`, with 3 scripts to configurate a strategy: `__init__.py` an empty file, `configuration.yaml` and `strategy.py`. See examples in `strategies/example/` and `strategies/another_example/`.

#### Example:

```bash
$ mkdir ./strategies/YOUR_STRATEGY_NAME   
$ touch ./strategies/YOUR_STRATEGY_NAME/\_\_init\_\_.py   
$ touch./strategies/YOUR_STRATEGY_NAME/configuration.yaml   
$ touch ./strategies/YOUR_STRATEGY_NAME/strategy.py   
```

TODO : explain how write `configuration.yaml` and `strategy.py`.

2. Start the bot manager server.

#### Example:

```bash
$ python ./trading_bot/bot_manager.py
```

3. Start the orders manager client.

#### Example:

```bash
$ python ./trading_bot/orders_manager.py
```

4. Start your stragies client.

#### Example:

```bash
$ python ./trading_bot/strategy_manager.py YOUR_STRATEGY_NAME
```

## Custom your own strategy manager

Documentation is available at [comming soon].

## TODO list

- General: 
    - Make a documentation;
- `bot_manager.py`:
    - Clean object;
    - Method: Run automatically order_manager client;
    - Method: Run automatically several strategies on multiprocess;
    - Method: Send update configuration to strategy manager.
- `orders_manager.py`:
    - Method: Verify integrity of new orders (if volume to order is available); 
    - Method: Algorithm of execution order (not just 'submit and leaves'). Something like vwap or twap. 
- `result_manager.py`:
    - Object: special logger to display pnl of strategies and portfolio
- `strategy_manager.py`:
    - Method: Receive update of configuration from bot_manager.
