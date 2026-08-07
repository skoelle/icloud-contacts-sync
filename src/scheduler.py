#!/usr/bin/env python3
# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Einfacher Cron-Scheduler fuer Sync und Mailer.
Ersetzt supercronic komplett — keine externe Abhaengigkeit noetig.

Laeuft als PID 1 im Docker-Container und plant:
  - Sync:  alle 15 Minuten (Delta-Sync)
  - Mailer: taeglich um MAIL_SEND_HOUR Uhr (falls MAILER_ENABLED=true)

Der Scheduler faengt SIGTERM/SIGINT ab und beendet sich sauber,
damit Docker den Container korrekt stoppen kann."""
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

import db
from config import Config

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("scheduler")

SYNC_INTERVAL_MINUTES = 15
MAIL_SEND_HOUR = int(os.environ.get("MAIL_SEND_HOUR", "7"))
MAILER_ENABLED = os.environ.get("MAILER_ENABLED", "false").lower() == "true"

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    signame = signal.Signals(signum).name
    logger.info("Signal %s empfangen, Scheduler faehrt herunter...", signame)
    _shutdown = True


def run_script(name: str, script_path: str):
    logger.info("--- %s-Lauf gestartet ---", name)
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            cwd="/app",
            timeout=300,
        )
        if result.returncode == 0:
            logger.info("%s-Lauf erfolgreich beendet", name)
        else:
            logger.error("%s-Lauf fehlgeschlagen (exit code %d)", name, result.returncode)
    except subprocess.TimeoutExpired:
        logger.error("%s-Lauf hat 5 Minuten ueberschritten, abgebrochen", name)
    except Exception:
        logger.exception("Unerwarteter Fehler waehrend %s-Lauf", name)


def run_sync():
    run_script("Sync", "/app/sync.py")
    try:
        with open("/tmp/last_sync_ok", "w") as f:
            f.write(datetime.now(Config.TIMEZONE).isoformat())
    except OSError:
        pass


def run_mailer():
    if not MAILER_ENABLED:
        return
    run_script("Mailer", "/app/mailer.py")


def next_run_time(hour: int) -> datetime:
    now = datetime.now(Config.TIMEZONE)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def main():
    global _shutdown

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Scheduler gestartet")
    logger.info("Sync: alle %d Minuten", SYNC_INTERVAL_MINUTES)
    if MAILER_ENABLED:
        logger.info("Mailer: taeglich um %d:00 Uhr", MAIL_SEND_HOUR)
    else:
        logger.info("Mailer: deaktiviert (MAILER_ENABLED=false)")

    logger.info("Pruefe/initialisiere Datenbank-Schema...")
    try:
        db.ensure_schema()
    except Exception:
        logger.exception("Schema-Init fehlgeschlagen — Sync wird trotzdem gestartet")

    logger.info("Starte ersten Sync-Lauf...")
    run_sync()

    last_sync = datetime.now(Config.TIMEZONE)
    next_mailer = next_run_time(MAIL_SEND_HOUR)

    while not _shutdown:
        now = datetime.now(Config.TIMEZONE)

        if now >= last_sync + timedelta(minutes=SYNC_INTERVAL_MINUTES):
            run_sync()
            last_sync = now

        if MAILER_ENABLED and now >= next_mailer:
            run_mailer()
            next_mailer = next_run_time(MAIL_SEND_HOUR)

        time.sleep(10)

    logger.info("Scheduler beendet")


if __name__ == "__main__":
    main()
