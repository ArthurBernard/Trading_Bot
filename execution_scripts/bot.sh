#!/bin/bash
#
# Script to run automatically a Python scripts and verify each 
# second that program didn't shutdown. 
# Only one parameters, `name_strategy` to execute the strategy.
#
# Example:
#
# ./bot.sh example
#

# Run `execution_strat.py` python script
python3 strategy_manager/main.py $1 >> bot_$1.log 2>&1 &

# Check the PID
script_pid=`ps -f | grep main.py\ $1 | grep -v grep | awk '{print $2}'`
echo "$1 has started, this pid is $script_pid"

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
        echo 'Program has stopped'
        let "i = i + 1"

        # Run `execution_strat.py` python script
        python3 strategy_manager/main.py $1 >> bot_$1.log 2>&1 &

        # Check the PID
        script_pid=`ps -f | grep main.py\ $1 | grep -v grep | awk '{print $2}'`
        echo "$1 has restarted, this new pid is $script_pid"

    fi
    # Sleep one second
    sleep 1
    # Set varaibles
    ts=`date +%s`
    # Check shutdown counter
    if [ $i -gt 5 ]; then
        # Stop loop
        let "ts = ts + 3600"
        # Send notification
        # TODO : send an email or alarm
        echo 'Program shutdown more than five times !'
    fi
done
