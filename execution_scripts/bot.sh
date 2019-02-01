#!/bin/bash
#
# Script to run automatically some python scripts and verify each 
# second that program doesn't shutdown. 
# Only one parameters, `name_strategy` to execute. The bot will go
# to search instruction in a file named `var_name_strategy.sh`.
#
# Example:
#
# ./bot.sh example
#

# TODO : Read parameters in `./var_strat.sh` <=> ./var_$1.sh

# Run `execution_strat.py` python script
python3 execution_strat.py $STRAT_NAME $UNDERLYING $FREQUENCY $PATH $EXTRA_PARAMS >> bot_$1.log 2>&1 &

# Check the PID
script_pid=`ps -f | grep execution_strat.py $STRAT_NAME $UNDERLYING $FREQUENCY | grep -v grep | awk '{print $2}'`

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
        # Run `execution_strat.py` python script
        python3 execution_strat.py $STRAT_NAME $UNDERLYING $FREQUENCY $PATH $EXTRA_PARAMS >> bot_$1.log 2>&1 &

        # Check the PID
        script_pid=`ps -f | grep execution_strat.py $STRAT_NAME $UNDERLYING $FREQUENCY | grep -v grep | awk '{print $2}'`

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
