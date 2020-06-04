#!/usr/bin/env bash

cd /script_home/storage_control

./storagefancontrol.py > /dev/null 2>&1 & disown

