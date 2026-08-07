#!/usr/bin/env python3
# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Sendet eine tägliche E-Mail mit Geburtstagskindern pro Account aus der
contacts-Tabelle. Wird per Cron einmal täglich um MAIL_SEND_HOUR aufgerufen.
Verhindert Doppelversand am selben Tag pro Account über die Tabelle
birthday_mail_log."""
import logging
import smtplib
import sys
from datetime import date, datetime
from email.message import EmailMessage

import db
from config import Config
from utils import fmt_birthday_age, fmt_birthday_short

logging.basicConfig(level=Config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mailer")


def fetch_todays_birthdays_for_account(conn, account_name: str, target_date: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, account, full_name, given_name, middle_name, family_name,
                     prefix, suffix, birthday, organization, photo_url
               FROM contacts
               WHERE account = %s
                 AND birthday IS NOT NULL
                 AND MONTH(birthday) = %s
                 AND DAY(birthday) = %s
               ORDER BY given_name, family_name, full_name""",
            (account_name, target_date.month, target_date.day),
        )
        rows = cur.fetchall()
    for b in rows:
        if not b.get("full_name"):
            b["full_name"] = db._build_full_name(b) or "(Kein Name)"
    return rows


def already_sent_today(conn, account: str) -> bool:
    today = datetime.now(Config.TIMEZONE).date()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM birthday_mail_log WHERE account = %s AND sent_date = %s",
            (account, today),
        )
        return cur.fetchone() is not None


def log_sent(conn, account: str, count: int):
    today = datetime.now(Config.TIMEZONE).date()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO birthday_mail_log (account, sent_date, contacts_count) VALUES (%s, %s, %s)",
            (account, today, count),
        )
    conn.commit()


def build_message(account_name: str, birthdays: list[dict], target_date: date | None = None) -> EmailMessage:
    today = target_date or datetime.now(Config.TIMEZONE).date()
    msg = EmailMessage()
    msg["From"] = Config.MAIL_FROM

    if not birthdays:
        msg["Subject"] = f"Geburtstage heute ({today.isoformat()}): keine"
        msg.set_content("Heute hat niemand aus deinen Kontakten Geburtstag.")
        return msg

    msg["Subject"] = f"Geburtstage heute ({today.isoformat()}): {len(birthdays)}"

    plain_lines = [f"Heutige Geburtstage ({today.isoformat()}):", ""]
    for b in birthdays:
        date_str = fmt_birthday_short(b["birthday"])
        age = fmt_birthday_age(b["birthday"], today)
        age_str = f" · {age} Jahre" if age is not None else ""
        line = f"- {b['full_name']}  · {date_str}{age_str}"
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
        date_str = fmt_birthday_short(b["birthday"])
        age = fmt_birthday_age(b["birthday"], today)
        age_str = f' <span style="white-space:nowrap;">· {age} Jahre</span>' if age is not None else ""
        contact_link = f"{Config.WEB_URL.rstrip('/')}/contacts/{b['id']}" if Config.WEB_URL else ""
        org_html = f'<div style="font-size:13px;color:#666;margin-top:4px;">{b["organization"]}</div>' if b.get("organization") else ""
        photo_cell = ""
        if b.get("photo_url"):
            photo_cell = f'<td width="96" valign="top" style="padding:16px 0 16px 20px;"><img src="{b["photo_url"]}" alt="" width="80" height="80" style="border-radius:50%;display:block;"></td>'
        if contact_link:
            content_html = f"""<a href="{contact_link}" style="color:#222;text-decoration:none;display:block;">
                <div style="font-size:16px;font-weight:600;color:#222;">{b["full_name"]}</div>
                {org_html}
                <div style="font-size:13px;color:#888;margin-top:4px;">🎂 {date_str}{age_str}</div>
              </a>"""
        else:
            content_html = f"""<div style="font-size:16px;font-weight:600;color:#222;">{b["full_name"]}</div>
              {org_html}
              <div style="font-size:13px;color:#888;margin-top:4px;">🎂 {date_str}{age_str}</div>"""
        cards_html += f"""
        <tr>
          <td style="padding:0 0 8px 0;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f0f0;border:1px solid #e0e0e0;">
              <tr>
                <td style="padding:12px 16px;">
                  {content_html}
                </td>
                {photo_cell}
              </tr>
            </table>
          </td>
        </tr>"""

    link_html = ""
    if Config.WEB_URL:
        link_html = f"""
          <tr>
            <td style="padding:24px 0 16px 0;" align="center">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="background:#0066cc;border-radius:4px;">
                    <a href="{Config.WEB_URL}" style="display:inline-block;padding:12px 24px;color:#ffffff;text-decoration:none;font-size:14px;font-weight:600;">Alle Kontakte ansehen</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <!--[if mso]>
  <style>body,table,td {{ font-family:Arial,sans-serif !important; }}</style>
  <![endif]-->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;">
    <tr>
      <td align="center" style="padding:32px 16px;">
        <table width="800" cellpadding="0" cellspacing="0" style="max-width:800px;width:100%;background:#ffffff;border:1px solid #e0e0e0;">
          <tr>
            <td style="padding:16px;">
              <h1 style="font-size:20px;font-weight:600;color:#222;margin:0;">Geburtstage heute</h1>
              <p style="font-size:13px;color:#666;margin:8px 0 16px 0;">{today.strftime('%d.%m.%Y')} · {len(birthdays)} Kontakte</p>
              <table width="100%" cellpadding="0" cellspacing="0">
                {cards_html}
              </table>
              {link_html}
            </td>
          </tr>
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

    accounts = Config.load_accounts()
    mail_accounts = [a for a in accounts if a.birthday_mail_to]
    if not mail_accounts:
        logger.info("Keine Accounts mit birthday_mail_to konfiguriert, überspringe Lauf")
        return 0

    today = datetime.now(Config.TIMEZONE).date()
    sent_count = 0
    errors = 0

    with db.get_connection() as conn:
        for account in mail_accounts:
            if already_sent_today(conn, account.name):
                logger.info("Geburtstagsmail für Account '%s' wurde heute bereits versendet, überspringe", account.name)
                continue

            birthdays = fetch_todays_birthdays_for_account(conn, account.name, today)
            if not birthdays:
                logger.info("Keine Geburtstage heute für Account '%s', überspringe", account.name)
                continue

            msg = build_message(account.name, birthdays, today)
            msg["To"] = account.birthday_mail_to
            try:
                send_message(msg)
            except Exception:
                logger.exception("Versand der Geburtstagsmail für Account '%s' fehlgeschlagen", account.name)
                errors += 1
                continue

            log_sent(conn, account.name, len(birthdays))
            sent_count += 1
            logger.info("Geburtstagsmail versendet für Account '%s': %d Kontakte", account.name, len(birthdays))

    logger.info("Mailer-Lauf abgeschlossen: %d Mails versendet, %d Fehler", sent_count, errors)
    return 1 if errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
