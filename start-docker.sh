#!/bin/bash

LOGFILE="/home/cuav/docker-boot.log"
echo "=== Boot script started: $(date) ===" >> "$LOGFILE"

echo "Stopping any running containers..." >> "$LOGFILE"
RUNNING=$(docker ps -q)
if [ -n "$RUNNING" ]; then
    docker stop $RUNNING >> "$LOGFILE" 2>&1
else
    echo "No containers running." >> "$LOGFILE"
fi

echo "Starting uas_streaming stack..." >> "$LOGFILE"
cd /home/cuav/uas_streaming || { echo "Directory not found!" >> "$LOGFILE"; exit 1; }
docker compose up -d >> "$LOGFILE" 2>&1

echo "=== Boot script finished: $(date) ===" >> "$LOGFILE"