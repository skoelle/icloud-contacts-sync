#!/bin/sh
set -e
cd /app
echo "$(date -Is) - Mailer-Lauf gestartet"
python3 /app/mailer.py
echo "$(date -Is) - Mailer-Lauf beendet"
