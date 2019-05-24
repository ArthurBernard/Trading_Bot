#!/bin/bash
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2019-02-01 19:05:50
# @Last modified by: ArthurBernard
# @Last modified time: 2019-05-24 23:09:56
#
# Script to manage several strategy bots.
#

# TODO : to finish
# TODO : something like that
# Set path
path=./execution_scripts

# List of strategy's PID
PID_table = ()

let "i = 0"

# Run all strategy stored in the specified file
while read -r strategy; do
	$path/bot.sh $strategy >> $path/$strategy.log 2>&1 &
	PID_table [$i]=ps -f | grep "$path/bot.sh\ $strategy" | grep -v grep | awk '{print $2}'
	let "i = i + 1"
done < $path/strategy_list_to_run.txt
# PID_table=()
# let "i = 0"
# for strat in strats_list_to_run; do
#     ./bot.sh $strat >> strat.log 2>&1 &
#     PID_table[$i]=`ps -f | grep "./bot.sh $strat" | grep -v grep | awk '{print $2}'`
#     let "i = i + 1"
# done

# Check if all strategy is always running
# while [ condition ]; do
#     for PID in PID_table; do
#         check if running, etc
#      done
# done