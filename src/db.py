"""MariaDB-Anbindung: Delta-Sync-Strategie (Upsert für Änderungen, gezieltes Löschen für Removals)."""
import logging
import uuid
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from config import Config

logger = logging.getLogger(__name__)


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
    with conn.cursor() as cur:
        for c in contacts:
            c["sync_run_id"] = run_id
            cols = list(c.keys())
            placeholders = ", ".join(["%s"] * len(cols))
            update_clause = ", ".join(f"{col}=VALUES({col})" for col in cols if col not in ("account", "uid"))
            sql = (
                f"INSERT INTO contacts ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_clause}"
            )
            cur.execute(sql, list(c.values()))
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


def replace_all_contacts_for_account(conn, account: str, contacts: list[dict], run_id: str):
    """Voller Re-Sync für einen Account (initialer Lauf oder Recovery nach ungültigem sync-token)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM contacts WHERE account = %s", (account,))
        for c in contacts:
            c["sync_run_id"] = run_id
            cols = ", ".join(c.keys())
            placeholders = ", ".join(["%s"] * len(c))
            cur.execute(f"INSERT INTO contacts ({cols}) VALUES ({placeholders})", list(c.values()))
    conn.commit()
    logger.info("Voller Re-Sync für Account %s abgeschlossen: %d Kontakte", account, len(contacts))
