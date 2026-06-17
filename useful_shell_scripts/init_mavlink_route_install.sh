#!/usr/bin/env bash

git clone https://github.com/Siya-Bhadu/mavlink-router.git
cd mavlink-router
chmod +x install_mavlink_router.sh
./install_mavlink_router.sh

# copies the file into the main directory of mavlink-router and then moves it to the /etc/mavlink-router directory
cd ../
sudo mkdir -p /etc/mavlink-router
sudo cp main.conf /etc/mavlink-router/main.conf