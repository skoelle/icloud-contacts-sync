#!/usr/bin/env python3
# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Einstiegspunkt für einen Sync-Lauf über alle konfigurierten Accounts.
Für jeden Account wird ein Delta-Sync per CardDAV sync-collection (RFC 6578)
durchgeführt. Beim allerersten Lauf eines Accounts (kein gespeicherter
sync-token) sowie nach einem vom Server abgelehnten Token erfolgt ein
vollständiger Re-Sync."""
import logging
import sys

import db
from carddav_client import ICLOUD_BASE_URL, CardDAVClient, SyncTokenInvalid
from config import Config
from vcard_parser import parse_vcard

logging.basicConfig(level=Config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync")


def sync_account(conn, account, href_to_uid_cache: dict):
    client = CardDAVClient(ICLOUD_BASE_URL, account.apple_email, account.apple_app_password)
    collection_url = client.discover_collection()

    stored_token = db.get_sync_token(conn, account.name)
    sync_type = "delta" if stored_token else "initial"
    run_id = db.start_sync_run(conn, account.name, sync_type)

    try:
        if not stored_token:
            logger.info("[%s] Kein sync-token vorhanden, führe initialen Full-Sync aus", account.name)
            raw_vcards, raw_etags = client.fetch_all_vcards(collection_url)
            contacts = []
            for v, etag in zip(raw_vcards, raw_etags):
                c = parse_vcard(v, account.name, etag=etag)
                if c:
                    contacts.append(c)
                else:
                    logger.warning("[%s] vCard konnte nicht geparst werden, überspringe", account.name)
            db.replace_all_contacts_for_account(conn, account.name, contacts, run_id)
            _, _, _, new_token = client.sync_collection(collection_url, None)
            if new_token:
                db.save_sync_token(conn, account.name, new_token)
            db.finish_sync_run(conn, run_id, "success", upserted=len(contacts), deleted=0)
            logger.info("[%s] Initialer Sync abgeschlossen: %d Kontakte", account.name, len(contacts))
            return

        try:
            changed_vcards, etags, deleted_hrefs, new_token = client.sync_collection(collection_url, stored_token)
        except SyncTokenInvalid:
            logger.warning("[%s] sync-token vom Server abgelehnt, führe vollen Re-Sync aus", account.name)
            db.clear_sync_token(conn, account.name)
            raw_vcards, raw_etags = client.fetch_all_vcards(collection_url)
            contacts = []
            for v, etag in zip(raw_vcards, raw_etags):
                c = parse_vcard(v, account.name, etag=etag)
                if c:
                    contacts.append(c)
                else:
                    logger.warning("[%s] vCard konnte nicht geparst werden, überspringe", account.name)
            db.replace_all_contacts_for_account(conn, account.name, contacts, run_id)
            _, _, _, new_token = client.sync_collection(collection_url, None)
            if new_token:
                db.save_sync_token(conn, account.name, new_token)
            db.finish_sync_run(conn, run_id, "success", upserted=len(contacts), deleted=0)
            logger.info("[%s] Re-Sync abgeschlossen: %d Kontakte", account.name, len(contacts))
            return

        contacts = []
        for v, etag in zip(changed_vcards, etags):
            c = parse_vcard(v, account.name, etag=etag)
            if c:
                contacts.append(c)
            else:
                logger.warning("[%s] vCard konnte nicht geparst werden, überspringe", account.name)
        db.upsert_contacts(conn, contacts, run_id)

        deleted_uids = []
        for href in deleted_hrefs:
            uid = href.rstrip("/").rsplit("/", 1)[-1].replace(".vcf", "")
            if uid:
                deleted_uids.append(uid)
            else:
                logger.warning("[%s] Konnte UID nicht aus href extrahieren: %s", account.name, href)
        db.delete_contacts_by_href_uids(conn, account.name, deleted_uids)

        if new_token:
            db.save_sync_token(conn, account.name, new_token)

        db.finish_sync_run(conn, run_id, "success", upserted=len(contacts), deleted=len(deleted_uids))
        logger.info(
            "[%s] Delta-Sync abgeschlossen: %d geändert/neu, %d gelöscht",
            account.name, len(contacts), len(deleted_uids),
        )
    except Exception as exc:
        logger.exception("[%s] Sync-Lauf %s fehlgeschlagen", account.name, run_id)
        db.finish_sync_run(conn, run_id, "failed", error_message=str(exc))
        raise


def main() -> int:
    try:
        Config.validate_db()
        accounts = Config.load_accounts()
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    exit_code = 0
    with db.get_connection() as conn:
        for account in accounts:
            try:
                sync_account(conn, account, {})
            except Exception:
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
