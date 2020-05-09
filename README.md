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
    - 3.7
    - 3.8

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

At the root of `Trading_Bot`:

### 1. Create a strategy:

Make a folder `./strategies/YOUR_STRATEGY_NAME` with 3 scripts to configurate the strategy: `__init__.py` an empty file, `configuration.yaml` and `strategy.py`. See examples in the following directory `./strategies/example/` and `./strategies/another_example/`.

```bash
$ mkdir ./strategies/YOUR_STRATEGY_NAME   
$ touch ./strategies/YOUR_STRATEGY_NAME/__init__.py   
$ touch./strategies/YOUR_STRATEGY_NAME/configuration.yaml   
$ touch ./strategies/YOUR_STRATEGY_NAME/strategy.py   
```

TODO : tuto how write `configuration.yaml` and `strategy.py`.

### 2. Start the bot manager server.

Set `./general_config.yaml` file (or let it as default) with:
  - `log_file`: the path of your log file.
  - `strategy`: the path where you save your custom strategy functions.
  - `address`: address of your server and port (can be local or remote).
  - `authkey`: password of your server.
  - `auto`: if true starts automatically the order manager and performance manager client, otherwise you must to run it mannually.

And run the trading bot:

```bash
$ python ./trading_bot/bot_manager.py > /dev/null 2>&1 &
```

If you have to choose to run mannually the order manager and trading performance manager clients:

```bash
$ python ./trading_bot/orders_manager.py &
$ python ./trading_bot/performance.py &
```

### 3. Manage trading bots with the CLI.

```bash
$ python ./trading_bot/cli.py
```

With the CLI you can start or stop one or several strategy bots, display KPI of the running strategy bots and stop the trading bot.

The following command lines are available:
- `q`: quite the command line interface.
- `stop`: stop the trading bot server and all the client strategies.
- `stop [STRATEGY_NAME]`: stop the strategy bot manager `STRATEGY_NAME`.
- `start [STRATEGY_NAME]`: start the strategy bot manager `STRATEGY_NAME`.
- `<ENTER>`: display the performance table of running strategies.

TODO: append more command lines (e.g dispaly plot of performance, force to execute a pending order (i.e if an order wasn't executed due to a bug), etc.)

### 4. Monitor loggers.

You can see the logs of trading bot in the files `debug.log` and `error.log`.

## Custom your own strategy manager

Documentation is available at [comming soon].

## Disclaimer

Do not risk money which you are afraid to lose.
Use the trading bot at your own risk, the authors assume no responsibility for your trading results.
Read the source code and make sure there are not undesirable behaviors.

## TODO list

- General: 
    - Make documentation and clean objects;
    - Improve Quick-Start;
    - Improve CLI/make GUI.
- `bot_manager.py`:
    - Start automatically several `StrategyManager` clients on several process;
- `_order.py`:
    - Use WebSockets instead of REST API.
- `strategy_manager.py`:
    - Choose invested value in quote or base currency.
