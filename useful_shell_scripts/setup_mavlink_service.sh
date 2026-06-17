#!/usr/bin/env bash

cd ../
sudo cp mavlink_router.service /etc/systemd/system/mavlink_router.service

echo "Mavlink Router service file has been copied to /etc/systemd/system/"

sudo systemctl daemon-reload
sudo systemctl enable mavlink-router.service
sudo systemctl start mavlink-router.service

echo "Mavlink Router service has been set up and started."
sudo systemctl status mavlink-router.service