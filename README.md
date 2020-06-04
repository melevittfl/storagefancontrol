storagefancontrol 
=================
Fan speed PID controller based on hard drive temperature
--------------------------------------------------------

This project was forked from a fan control script built for Linux. It has been 
modified to work on FreeNAS 11 and using an ASRock Rack motherboard (specifically the E3C236D4U. Other models may differ).

This script is meant for storage servers with lots of (spinning) hard drives.
It regulates the chassis (PWM) fan speed based on the hard drive temperature. 

The script is intended for people who build large storage servers used in an
environment (at home) where noise matters.

The hard drive temperature is monitored through SMART.

Fan speed is governed by PWM fan controls and sensors as supported the ipmitool.

The ASRock Rack motherboard fan speed is controlled by the following command

```
ipmitool raw 0x3a 0x01 0x64 0x00 0x64 0x00 0x64 0x64 0x00 0x00
		       CPU	 REAR	   FRNT1 FRNT2

0x00 is Auto
0x01 is Min
0x64 is Max
```


This script has been updated to handle multiple PWM devices.

Fan control is coverned by the control loop feedback mechanism [PID][pid].
Here is a [nice intro][video01] on PID. By using PID, the script always finds
the optimal fan speed no matter what the circumstances are.

[video01]: https://www.youtube.com/watch?v=UR0hOmjaHp0
[pid]: http://en.wikipedia.org/wiki/PID_controller  

For example, if you have 24 drives in a chassis, this script checks the temperature
of each drive. The temperature of the hottest drive is used to determine if the 
chassis fans need to run faster, slower or if they should stay at the same speed.

the PID controller makes sure that an optimal fan speed is found to keep the
system at a maximum of - in my case - 40C. The target temp is 
configurable.

The script logs internal variables to syslog by default.

    Temp: 40 | FAN: 51% | PWM: 130 | P=0   | I=51  | D=0   | Err=0  |
    Temp: 40 | FAN: 51% | PWM: 130 | P=0   | I=51  | D=0   | Err=0  |
    Temp: 40 | FAN: 51% | PWM: 130 | P=0   | I=51  | D=0   | Err=0  |
    Temp: 40 | FAN: 51% | PWM: 130 | P=0   | I=51  | D=0   | Err=0  |
    Temp: 39 | FAN: 43% | PWM: 109 | P=-2  | I=50  | D=-5  | Err=-1 |
    Temp: 39 | FAN: 47% | PWM: 119 | P=-2  | I=49  | D=0   | Err=-1 |
    Temp: 40 | FAN: 54% | PWM: 137 | P=0   | I=49  | D=5   | Err=0  |
    Temp: 40 | FAN: 49% | PWM: 124 | P=0   | I=49  | D=0   | Err=0  |
    Temp: 40 | FAN: 49% | PWM: 124 | P=0   | I=49  | D=0   | Err=0  |
    Temp: 40 | FAN: 49% | PWM: 124 | P=0   | I=49  | D=0   | Err=0  |

This will give you output on the console:

    export DEBUG=True 

The disk temperature is read through 'smartctl' (part of smartmontools).


The script performs a poll every 30 seconds by default. 


Forked From: https://github.com/louwrentius/storagefancontrol

INSTALL
--------
1. Copy the configuration file and the script where you want
2. Check the config file
3. Make sure the script is executed on boot.

TODO
----
Clean up references to MegaCLI.