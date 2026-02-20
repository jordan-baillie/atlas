#!/bin/bash
# Dashboard keepalive - restarts Flask server if not responding
PYTHON=/opt/venv/bin/python
PROJECT=/a0/usr/projects/atlas-asx
LOG=$PROJECT/logs/keepalive.log

# Check if dashboard responds
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/ 2>/dev/null)

if [ "$HTTP_CODE" != "200" ]; then
    echo "$(date): Dashboard down (HTTP $HTTP_CODE), restarting..." >> $LOG
    pkill -f 'dashboard/live_api.py' 2>/dev/null
    sleep 1
    cd $PROJECT
    nohup $PYTHON dashboard/live_api.py > dashboard/server.log 2>&1 &
    sleep 3
    NEW_CODE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/ 2>/dev/null)
    echo "$(date): Restart result: HTTP $NEW_CODE (PID $!)" >> $LOG
fi
