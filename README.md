# SATCOM Streamer
- This repo is used to stream telemetry of a UAS via mavlink-router protocol
- In addition video is provided using mediamtx 

# Prequsites
- The assumption for this repo is that you have a VPN installed whether Tailscale or OpenVpn to route telemetry and video accordingly
- In addition you must have docker and docker compose installed to use mediamtx 

# Mavlink-Router

# Quick Install Commands
``` bash
cd useful_shell_scripts/
./init_mavlink_router_install.sh
cd useful_shell_scripts/
./setup_mavlink_service.sh
cd useful_shell_scripts/
./view_mavlink_service.sh
```

## Installation and Setup of Mavlink Router
We provide some useful_shell_scripts to automate the installation of mavlink-router to install mavlink_router and get it configured correctly, first look at the main.conf file and change the ingestion point and output correctly 
```conf
[General]
TcpServerPort=0
MavlinkDialect=ardupilotmega

[UartEndpoint FC]
Device=/dev/ttyACM0
Baud=57600

[UdpEndpoint Laptop]
Mode=Normal
Address=100.106.38.39
Port=14550

# Justin's Tailscale IP
[UdpEndpoint Justin]
Mode=Normal
Address=100.91.210.67
Port=14550

# Ground station
[UdpEndpoint GroundStation]
Mode=Normal
Address=100.92.248.58
Port=14550

#To SEME
[UdpEndpoint SEME]
Mode=Normal
Address=100.106.38.39
Port=14550
```
Once you have made the appropriate changes do the following
```bash
cd useful_shellscripts
./init_mavlink_route_install.sh
```
The shell script will copy the main.conf into the /etc/mavlink-router/main.conf to begin bridging information

To begin routing the mavlink information run
```
./run_mavlink_router.sh
```
This will begin the routing but if the pi gets rebooted or dies then the routing will end to set this up a service look at the next section

## Setup Mavlink-Router as a service
- To make this a service where this runs on boot and will restart if the router dies we provide a mavlink_router.servie script 
- Make sure to change the ExecStart directory to where you see fit to make this work
```ini
[Unit]
Description=MAVLink Router Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/home/pi/run_mavlink_router.sh #change this to your respective repo
Restart=always
RestartSec=2
User=root

[Install]
WantedBy=multi-user.target
```
- Once you have set up the right directory you can start the service by entering the following
```
cd useful_shell_scripts/
./setup_mavlink_service.sh
```

# Media Streaming
