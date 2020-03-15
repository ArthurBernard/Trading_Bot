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
    - blessed
    - fynance
    - numpy
    - pandas
    - requests

## Installation

At the root of a folder, clone the repository and install it with `pip`:

```bash
$ git clone https://github.com/ArthurBernard/Trading_Bot.git    
$ cd Trading_Bot    
$ pip install -e trading_bot   
```

## Quick-start

1. Create a folder `./strategies/YOUR_STRATEGY_NAME`, with 3 scripts to configurate a strategy: `__init__.py` an empty file, `configuration.yaml` and `strategy.py`. See examples in `strategies/example/` and `strategies/another_example/`.

#### Example:

```bash
$ mkdir ./strategies/YOUR_STRATEGY_NAME   
$ touch ./strategies/YOUR_STRATEGY_NAME/__init__.py   
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

## Disclaimer

Do not risk money which you are afraid to lose.
Use the trading bot at your own risk, the authors assume no responsibility for your trading results.
Read the source code and make sure there are not undesirable behaviors.

## TODO list

- General: 
    - Make documentation;
    - Simplify Quick-Start;
    - Make GUI.
- `bot_manager.py`:
    - Clean object;
    - Start automatically several `StrategyManager` clients on several process;
- `orders_manager.py`:
    - Verify if the volume to order is available; 
- `_order.py`:
    - Use WebSockets instead of REST API.
- `result_manager.py`:
    - Make an object to display PnL of strategies and portfolio.
- `strategy_manager.py`:
    - Choose invested value in quote or base currency.
