#!/bin/bash
#
# Script to manage several bots
#

# TODO 
# something like that
# PID_table=()
# let "i = 0"
# for strat in strats_list_to_run; do
#     ./bot.sh $strat >> strat.log 2>&1 &
#     PID_table[$i]=`ps -f | grep "./bot.sh $strat" | grep -v grep | awk '{print $2}'`
#     let "i = i + 1"
# done
# while [ condition ]; do
#     for PID in PID_table; do
#         check if running, etc
#      done
# done