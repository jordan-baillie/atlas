#!/bin/bash
# Atlas-ASX startup script - runs on boot
PYTHON=/opt/venv/bin/python
PROJECT=/a0/usr/projects/atlas-asx
LOG=$PROJECT/logs/startup.log

echo "$(date): Atlas-ASX starting up..." >> $LOG

# Start dashboard live API server
pkill -f 'dashboard/live_api.py' 2>/dev/null
sleep 1
cd $PROJECT
nohup $PYTHON dashboard/live_api.py > dashboard/server.log 2>&1 &
echo "$(date): Dashboard started (PID $!)" >> $LOG

# Wait and verify
sleep 3
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/ 2>/dev/null)
echo "$(date): Dashboard health: HTTP $HTTP_CODE" >> $LOG
