#!/usr/bin/env python3
# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Sendet eine tägliche E-Mail mit allen heutigen Geburtstagskindern aus der
contacts-Tabelle (über alle Accounts hinweg). Wird per Cron einmal täglich
um MAIL_SEND_HOUR aufgerufen. Verhindert Doppelversand am selben Tag über
die Tabelle birthday_mail_log."""
import logging
import smtplib
import sys
from datetime import date, datetime
from email.message import EmailMessage

import db
from config import TIMEZONE, Config

logging.basicConfig(level=Config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mailer")


def fetch_birthdays_for_date(conn, target_date: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, account, full_name, birthday, organization, photo_url
               FROM contacts
               WHERE birthday IS NOT NULL
                 AND MONTH(birthday) = %s
                 AND DAY(birthday) = %s
               ORDER BY full_name""",
            (target_date.month, target_date.day),
        )
        return cur.fetchall()


def fetch_todays_birthdays(conn) -> list[dict]:
    return fetch_birthdays_for_date(conn, datetime.now(TIMEZONE).date())


def already_sent_today(conn) -> bool:
    today = datetime.now(TIMEZONE).date()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM birthday_mail_log WHERE sent_date = %s", (today,))
        return cur.fetchone() is not None


def log_sent(conn, count: int):
    today = datetime.now(TIMEZONE).date()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO birthday_mail_log (sent_date, contacts_count) VALUES (%s, %s)",
            (today, count),
        )
    conn.commit()


def build_message(birthdays: list[dict], target_date: date | None = None) -> EmailMessage:
    today = target_date or datetime.now(TIMEZONE).date()
    msg = EmailMessage()
    msg["From"] = Config.MAIL_FROM
    msg["To"] = Config.MAIL_TO

    if not birthdays:
        msg["Subject"] = f"Geburtstage heute ({today.isoformat()}): keine"
        msg.set_content("Heute hat niemand aus deinen Kontakten Geburtstag.")
        return msg

    msg["Subject"] = f"Geburtstage heute ({today.isoformat()}): {len(birthdays)}"

    plain_lines = [f"Heutige Geburtstage ({today.isoformat()}):", ""]
    for b in birthdays:
        age = today.year - b["birthday"].year
        date_str = b["birthday"].strftime("%d.%m.")
        line = f"- {b['full_name']}  · {date_str} · {age} Jahre"
        if b.get("organization"):
            line += f"  ({b['organization']})"
        if Config.WEB_URL:
            line += f"  → {Config.WEB_URL.rstrip('/')}/contacts/{b['id']}"
        plain_lines.append(line)
    if Config.WEB_URL:
        plain_lines.extend(["", f"Alle Kontakte ansehen: {Config.WEB_URL}"])
    msg.set_content("\n".join(plain_lines))

    cards_html = ""
    for b in birthdays:
        age = today.year - b["birthday"].year
        date_str = b["birthday"].strftime("%d.%m.")
        contact_link = f"{Config.WEB_URL.rstrip('/')}/contacts/{b['id']}" if Config.WEB_URL else ""
        name_html = f'<a href="{contact_link}" style="color:#222;text-decoration:none;">{b["full_name"]}</a>' if contact_link else b["full_name"]
        is_today = b["birthday"].month == today.month and b["birthday"].day == today.day
        card_bg = "#fff3cd" if is_today else "#fff"
        org_html = f'<div style="font-size:14px;color:#666;margin-top:4px;">{b["organization"]}</div>' if b.get("organization") else ""
        photo_html = ""
        if b.get("photo_url"):
            photo_html = f'<img src="{b["photo_url"]}" alt="" style="width:80px;height:80px;border-radius:50%;object-fit:cover;flex-shrink:0;">'
        cards_html += f"""
        <tr>
          <td style="background:{card_bg};border-radius:8px;padding:1rem 1.25rem;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;">
              <div>
                <div style="font-size:18px;font-weight:600;color:#222;">{name_html}</div>
                {org_html}
                <div style="font-size:14px;color:#888;margin-top:4px;">🎂 {date_str} · {age} Jahre</div>
              </div>
              {photo_html}
            </div>
          </td>
        </tr>
        <tr><td style="height:12px;"></td></tr>"""

    link_html = ""
    if Config.WEB_URL:
        link_html = f"""
          <tr>
            <td style="padding-top:24px;text-align:center;">
              <a href="{Config.WEB_URL}" style="display:inline-block;padding:12px 24px;background:#0066cc;color:#fff;text-decoration:none;border-radius:6px;font-size:14px;">Alle Kontakte ansehen</a>
            </td>
          </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;">
    <tr>
      <td align="center" style="padding:2rem 1rem;">
        <table width="800" cellpadding="0" cellspacing="0" style="max-width:800px;width:100%;">
          <tr>
            <td>
              <h1 style="font-size:24px;font-weight:600;color:#222;margin:0 0 8px 0;">Geburtstage heute</h1>
              <p style="font-size:14px;color:#666;margin:0 0 24px 0;">{today.strftime('%d.%m.%Y')} · {len(birthdays)} Kontakte</p>
            </td>
          </tr>
          <tr>
            <td>
              <table width="100%" cellpadding="0" cellspacing="0">
                {cards_html}
              </table>
            </td>
          </tr>
          {link_html}
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    msg.add_alternative(html, subtype="html")
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
        if not birthdays:
            logger.info("Keine Geburtstage heute, überspringe Mailversand")
            return 0

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
