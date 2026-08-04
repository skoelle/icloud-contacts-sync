#!/bin/sh
set -e
cd /app
echo "$(date -Is) - Sync-Lauf gestartet"
python3 /app/sync.py
echo "$(date -Is) - Sync-Lauf beendet"
touch /tmp/last_sync_ok
