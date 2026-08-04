#!/bin/sh
set -e

# Wurde ein alternativer Befehl uebergeben (z.B. uvicorn), fuehre diesen
# direkt aus und ueberspringe den Sync-/Cron-Modus.
if [ $# -gt 0 ]; then
    exec "$@"
fi

echo "Starte icloud-contacts-sync Container"
echo "Scheduler: Python-basiert (kein supercronic)"

exec python3 /app/scheduler.py
