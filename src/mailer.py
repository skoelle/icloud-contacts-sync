#!/usr/bin/env python3
"""Sendet eine tägliche E-Mail mit allen heutigen Geburtstagskindern aus der
contacts-Tabelle (über alle Accounts hinweg). Wird per Cron einmal täglich
um MAIL_SEND_HOUR aufgerufen. Verhindert Doppelversand am selben Tag über
die Tabelle birthday_mail_log."""
import logging
import smtplib
import sys
from datetime import date
from email.message import EmailMessage

from config import Config
import db

logging.basicConfig(level=Config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mailer")


def fetch_todays_birthdays(conn) -> list[dict]:
    today = date.today()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT account, full_name, birthday
               FROM contacts
               WHERE birthday IS NOT NULL
                 AND MONTH(birthday) = %s
                 AND DAY(birthday) = %s
               ORDER BY full_name""",
            (today.month, today.day),
        )
        return cur.fetchall()


def already_sent_today(conn) -> bool:
    today = date.today()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM birthday_mail_log WHERE sent_date = %s", (today,))
        return cur.fetchone() is not None


def log_sent(conn, count: int):
    today = date.today()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO birthday_mail_log (sent_date, contacts_count) VALUES (%s, %s)",
            (today, count),
        )
    conn.commit()


def build_message(birthdays: list[dict]) -> EmailMessage:
    today = date.today()
    msg = EmailMessage()
    msg["From"] = Config.MAIL_FROM
    msg["To"] = Config.MAIL_TO

    if not birthdays:
        msg["Subject"] = f"Geburtstage heute ({today.isoformat()}): keine"
        msg.set_content("Heute hat niemand aus deinen Kontakten Geburtstag.")
        return msg

    msg["Subject"] = f"Geburtstage heute ({today.isoformat()}): {len(birthdays)}"
    lines = [f"Heutige Geburtstage ({today.isoformat()}):", ""]
    for b in birthdays:
        age = today.year - b["birthday"].year
        lines.append(f"- {b['full_name']} (wird {age}, Account: {b['account']})")
    msg.set_content("\n".join(lines))
    return msg


def send_message(msg: EmailMessage):
    if Config.SMTP_USE_TLS:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
            server.starttls()
            if Config.SMTP_USER:
                server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
            if Config.SMTP_USER:
                server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            server.send_message(msg)


def main() -> int:
    if not Config.MAILER_ENABLED:
        logger.info("Mailer ist deaktiviert (MAILER_ENABLED=false), überspringe Lauf")
        return 0

    try:
        Config.validate_db()
        Config.validate_mailer()
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    with db.get_connection() as conn:
        if already_sent_today(conn):
            logger.info("Geburtstagsmail wurde heute bereits versendet, überspringe")
            return 0

        birthdays = fetch_todays_birthdays(conn)
        msg = build_message(birthdays)
        try:
            send_message(msg)
        except Exception:
            logger.exception("Versand der Geburtstagsmail fehlgeschlagen")
            return 1

        log_sent(conn, len(birthdays))
        logger.info("Geburtstagsmail versendet: %d Kontakte", len(birthdays))
    return 0


if __name__ == "__main__":
    sys.exit(main())
