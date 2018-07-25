#!/usr/bin/env bash

cd /mnt/tank/home/mlevitt/storage_control

./storagefancontrol.py > /dev/null 2>&1 & disown

