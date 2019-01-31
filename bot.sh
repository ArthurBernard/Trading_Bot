#!/bin/bash
#
# Script to run automatically some python scripts and verify each 
# second that program doesn't shutdown. First parameter is name file 
# of python script to execute (e.g strategy_manager.py) and following 
# parameters are parameters for python script (e.g strategy_name, 
# underlying, and any additional parameters).
#
# Exemple:
#
# ./bot.sh script.py para1 para2 etc.
#
# TODO : Modify parameters setting: Parameter `instruction_file` where
# are setting which strategy to execute with wich parameters (e.g. 
# strategy_name, underlying, path/save_data, path/error, etc.).

# Run python script 
python3 $1 $2 $3 >> out/$1.out 2>> error/$1.log&

# Check the PID
script_pid=`ps -f | grep $1 | grep -v grep | awk '{print $2}'`

# Define curent timestamp
ts=`date +%s`

# Define stop 
let "stop = ts - ts % 3600 + 3570"

# Define shutdown counter
let "i = 0"

# Loop while an hour
while [ $ts -lt $stop ]; do
    # Instructions
    # Check if script is always running
    if ! ps -p $script_pid > /dev/null; then
        # Program shutdown
        let "i = i + 1"
        # Run python script to download ohlc data
        python3 $1 $2 $3 >> out/$1.out 2>> error/$1.log&
        # Check the PID
        script_pid=`ps -f | grep $1 | grep -v grep | awk '{print $2}'`
    fi
    # Sleep one second
    sleep 1
    # Set varaibles
    ts=`date +%s`
    # Check shutdown counter
    if [ i -gt 5 ]; do
        # Stop loop
        let "ts = ts + 3600"
        # Send notification
        # TODO : send an email or alarm
    fi
done
