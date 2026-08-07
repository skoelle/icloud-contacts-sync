# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""MariaDB-Anbindung: Delta-Sync-Strategie (Upsert für Änderungen, gezieltes Löschen für Removals)."""
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pymysql
from pymysql.cursors import DictCursor

from config import Config

logger = logging.getLogger(__name__)

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "sql", "schema.sql")


@contextmanager
def get_connection():
    conn = pymysql.connect(
        host=Config.MARIADB_HOST, port=Config.MARIADB_PORT,
        user=Config.MARIADB_USER, password=Config.MARIADB_PASSWORD,
        database=Config.MARIADB_DATABASE, cursorclass=DictCursor, autocommit=False,
    )
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema():
    """Legt alle Tabellen an, falls sie noch nicht existieren (idempotent)."""
    schema_file = os.path.normpath(SCHEMA_PATH)
    if not os.path.exists(schema_file):
        logger.warning("Schema-Datei nicht gefunden: %s — überspringe Init", schema_file)
        return
    with open(schema_file, "r", encoding="utf-8") as f:
        sql = f.read()
    with get_connection() as conn:
        with conn.cursor() as cur:
            for statement in sql.split(";"):
                statement = statement.strip()
                if statement:
                    cur.execute(statement)
        conn.commit()
    logger.info("Datenbank-Schema geprüft/initialisiert")


def get_sync_token(conn, account: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT sync_token FROM sync_state WHERE account = %s", (account,))
        row = cur.fetchone()
    return row["sync_token"] if row else None


def save_sync_token(conn, account: str, sync_token: str):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sync_state (account, sync_token) VALUES (%s, %s)
               ON DUPLICATE KEY UPDATE sync_token = %s""",
            (account, sync_token, sync_token),
        )
    conn.commit()


def clear_sync_token(conn, account: str):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sync_state WHERE account = %s", (account,))
    conn.commit()


def start_sync_run(conn, account: str, sync_type: str) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_runs (id, account, sync_type, status) VALUES (%s, %s, %s, 'running')",
            (run_id, account, sync_type),
        )
    conn.commit()
    return run_id


def finish_sync_run(conn, run_id: str, status: str, upserted: int = None, deleted: int = None, error_message: str = None):
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE sync_runs SET status=%s, contacts_upserted=%s, contacts_deleted=%s,
               error_message=%s, finished_at=NOW() WHERE id=%s""",
            (status, upserted, deleted, error_message, run_id),
        )
    conn.commit()


def upsert_contacts(conn, contacts: list[dict], run_id: str):
    if not contacts:
        return
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        for c in contacts:
            c["sync_run_id"] = run_id
            c["last_synced_at"] = now
            c = _sanitize_contact(c)
            cols = list(c.keys())
            placeholders = ", ".join(["%s"] * len(cols))
            update_clause = ", ".join(f"{col}=VALUES({col})" for col in cols if col not in ("account", "uid"))
            sql = (
                f"INSERT INTO contacts ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_clause}"
            )
            try:
                cur.execute(sql, list(c.values()))
            except Exception as exc:
                logger.error("INSERT fehlgeschlagen für UID %s: %s", c.get("uid"), exc)
                logger.error("SQL: %s", sql[:500])
                logger.error("Values: %s", {k: v for k, v in c.items() if k != "raw_vcard"})
                raise
    conn.commit()


def delete_contacts_by_href_uids(conn, account: str, uids: list[str]):
    if not uids:
        return
    with conn.cursor() as cur:
        placeholders = ", ".join(["%s"] * len(uids))
        cur.execute(
            f"DELETE FROM contacts WHERE account = %s AND uid IN ({placeholders})",
            [account] + uids,
        )
    conn.commit()


def delete_group_members_by_uids(conn, account: str, uids: list[str]):
    if not uids:
        return
    with conn.cursor() as cur:
        placeholders = ", ".join(["%s"] * len(uids))
        cur.execute(
            f"""DELETE gm FROM group_members gm
                JOIN `groups` g ON g.id = gm.group_id
                WHERE g.account = %s AND gm.member_uid IN ({placeholders})""",
            [account] + uids,
        )
    conn.commit()


def _build_full_name(row: dict) -> str | None:
    parts = [
        row.get("prefix"),
        row.get("given_name"),
        row.get("middle_name"),
        row.get("family_name"),
        row.get("suffix"),
    ]
    return " ".join(p for p in parts if p) or None


def _account_filter_clause(account_name: str | None) -> tuple[str, list]:
    if account_name is None:
        return "", []
    return "WHERE account = %s", [account_name]


def get_contact_count(conn, account: str | None) -> int:
    where_clause, params = _account_filter_clause(account)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM contacts {where_clause}", params)
        return cur.fetchone()["total"]


def search_contacts_without_photo(conn, account: str | None) -> list[dict]:
    where_clause, params = _account_filter_clause(account)
    op = "AND" if where_clause else "WHERE"
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT id, full_name, given_name, middle_name, family_name,
                       prefix, suffix, organization, birthday, account, photo_url
                FROM contacts {where_clause}
                {op} given_name IS NOT NULL AND given_name != ''
                AND family_name IS NOT NULL AND family_name != ''
                AND photo_base64 IS NULL AND photo_url IS NULL
                AND EXISTS (
                    SELECT 1 FROM JSON_TABLE(addresses, '$[*]' COLUMNS (city VARCHAR(255) PATH '$.city')) AS addr
                    WHERE addr.city IS NOT NULL AND addr.city != ''
                )
                AND family_name != 'X'
                ORDER BY given_name, family_name, full_name""",
            params,
        )
        return cur.fetchall()


def search_contacts_without_city(conn, account: str | None) -> list[dict]:
    where_clause, params = _account_filter_clause(account)
    op = "AND" if where_clause else "WHERE"
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT id, full_name, given_name, middle_name, family_name,
                       prefix, suffix, organization, birthday, account, photo_url
                FROM contacts {where_clause}
                {op} given_name IS NOT NULL AND given_name != ''
                AND family_name IS NOT NULL AND family_name != ''
                AND (JSON_LENGTH(phones) > 0 OR JSON_LENGTH(emails) > 0)
                AND NOT EXISTS (
                    SELECT 1 FROM JSON_TABLE(addresses, '$[*]' COLUMNS (city VARCHAR(255) PATH '$.city')) AS addr
                    WHERE addr.city IS NOT NULL AND addr.city != ''
                )
                AND family_name != 'X'
                ORDER BY given_name, family_name, full_name""",
            params,
        )
        return cur.fetchall()


def search_contacts_without_social(conn, account: str | None) -> list[dict]:
    where_clause, params = _account_filter_clause(account)
    op = "AND" if where_clause else "WHERE"
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT id, full_name, given_name, middle_name, family_name,
                       prefix, suffix, organization, birthday, account, photo_url
                FROM contacts {where_clause}
                {op} given_name IS NOT NULL AND given_name != ''
                AND family_name IS NOT NULL AND family_name != ''
                AND (JSON_LENGTH(phones) > 0 OR JSON_LENGTH(emails) > 0)
                AND (social_profiles IS NULL OR JSON_LENGTH(social_profiles) = 0)
                AND family_name != 'X'
                ORDER BY given_name, family_name, full_name""",
            params,
        )
        return cur.fetchall()


def get_upcoming_birthdays(conn, account: str | None, days: int = 7) -> list[dict]:
    where_clause, params = _account_filter_clause(account)
    today = datetime.now(Config.TIMEZONE).date()
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT id, full_name, given_name, middle_name, family_name,
                       prefix, suffix, organization, birthday, account, photo_url
                FROM contacts {where_clause}
                {"AND" if where_clause else "WHERE"} birthday IS NOT NULL
                AND (
                    (MONTH(birthday) > %s)
                    OR (MONTH(birthday) = %s AND DAY(birthday) >= %s)
                )
                AND (
                    (MONTH(birthday) < %s)
                    OR (MONTH(birthday) = %s AND DAY(birthday) <= %s + %s)
                )
                ORDER BY MONTH(birthday), DAY(birthday), given_name, family_name, full_name""",
            params + [today.month, today.month, today.day,
                       today.month, today.month, today.day, days],
        )
        rows = cur.fetchall()
    for row in rows:
        if not row.get("full_name"):
            row["full_name"] = _build_full_name(row)
    return rows


def _sanitize_contact(c: dict) -> dict:
    """Stellt sicher, dass alle Werte Skalare sind (kein tuple/list/dict)."""
    sanitized = {}
    for k, v in c.items():
        if isinstance(v, (list, tuple)):
            # JSON-Felder sind bereits serialisiert, andere zu Strings machen
            if k in ("emails", "phones", "addresses", "urls", "social_profiles", "related_names", "categories"):
                sanitized[k] = v  # bereits JSON-String
            else:
                sanitized[k] = ",".join(str(x) for x in v) if v else None
        elif isinstance(v, dict):
            sanitized[k] = str(v) if v else None
        else:
            sanitized[k] = v
    return sanitized


def replace_all_contacts_for_account(conn, account: str, contacts: list[dict], run_id: str):
    """Voller Re-Sync für einen Account (initialer Lauf oder Recovery nach ungültigem sync-token)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM contacts WHERE account = %s", (account,))
        for c in contacts:
            c["sync_run_id"] = run_id
            c = _sanitize_contact(c)
            cols = ", ".join(c.keys())
            placeholders = ", ".join(["%s"] * len(c))
            try:
                cur.execute(f"INSERT INTO contacts ({cols}) VALUES ({placeholders})", list(c.values()))
            except Exception as exc:
                logger.error("INSERT fehlgeschlagen für UID %s: %s", c.get("uid"), exc)
                logger.error("Values: %s", {k: v for k, v in c.items() if k != "raw_vcard"})
                raise
    conn.commit()
    logger.info("Voller Re-Sync für Account %s abgeschlossen: %d Kontakte", account, len(contacts))


def upsert_groups(conn, groups: list[dict], run_id: str):
    if not groups:
        return
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        for g in groups:
            g["sync_run_id"] = run_id
            g["last_synced_at"] = now
            member_uids = g.pop("member_uids", [])
            cols = [k for k in g if k != "member_uids"]
            placeholders = ", ".join(["%s"] * len(cols))
            update_clause = ", ".join(f"{col}=VALUES({col})" for col in cols if col not in ("account", "uid"))
            sql = (
                f"INSERT INTO `groups` ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_clause}"
            )
            try:
                cur.execute(sql, [g[c] for c in cols])
            except Exception as exc:
                logger.error("INSERT Gruppe fehlgeschlagen für UID %s: %s", g.get("uid"), exc)
                raise
            cur.execute("SELECT id FROM `groups` WHERE account = %s AND uid = %s", (g["account"], g["uid"]))
            row = cur.fetchone()
            if not row:
                logger.error("Konnte Gruppen-ID nicht ermitteln für UID %s", g.get("uid"))
                continue
            group_id = row["id"]
            cur.execute("DELETE FROM group_members WHERE group_id = %s", (group_id,))
            if member_uids:
                member_values = [(group_id, uid) for uid in member_uids]
                cur.executemany(
                    "INSERT INTO group_members (group_id, member_uid) VALUES (%s, %s)",
                    member_values,
                )
    conn.commit()


def delete_groups_by_uids(conn, account: str, uids: list[str]):
    if not uids:
        return
    with conn.cursor() as cur:
        placeholders = ", ".join(["%s"] * len(uids))
        cur.execute(
            f"DELETE FROM `groups` WHERE account = %s AND uid IN ({placeholders})",
            [account] + uids,
        )
    conn.commit()


def replace_all_groups_for_account(conn, account: str, groups: list[dict], run_id: str):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM `groups` WHERE account = %s", (account,))
    conn.commit()
    if groups:
        upsert_groups(conn, groups, run_id)
    logger.info("Gruppen-Re-Sync für Account %s abgeschlossen: %d Gruppen", account, len(groups))


def get_groups_for_contact(conn, account: str, member_uid: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT g.id, g.name, g.uid
               FROM `groups` g
               JOIN group_members gm ON gm.group_id = g.id
               WHERE g.account = %s AND gm.member_uid = %s
               ORDER BY g.name""",
            (account, member_uid),
        )
        return cur.fetchall()


def get_group_count(conn, account: str | None) -> int:
    where_clause, params = _account_filter_clause(account)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM `groups` {where_clause}", params)
        return cur.fetchone()["total"]


def get_all_groups(conn, account: str | None) -> list[dict]:
    where_clause, params = _account_filter_clause(account)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, name, uid FROM `groups` {where_clause} ORDER BY name",
            params,
        )
        return cur.fetchall()


def get_contacts_by_group_uid(conn, account: str | None, group_uid: str) -> list[dict]:
    where_clause, params = _account_filter_clause(account)
    group_op = "AND" if where_clause else "WHERE"
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT c.id, c.full_name, c.given_name, c.middle_name, c.family_name,
                       c.prefix, c.suffix, c.organization, c.birthday, c.account, c.photo_url
                FROM contacts c
                JOIN group_members gm ON gm.member_uid = c.uid
                JOIN `groups` g ON g.id = gm.group_id AND g.account = c.account
                {where_clause} {group_op} g.uid = %s
                ORDER BY c.given_name, c.family_name, c.full_name""",
            params + [group_uid],
        )
        return cur.fetchall()
