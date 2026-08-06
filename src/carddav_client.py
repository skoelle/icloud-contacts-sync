# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""
CardDAV-Client für iCloud (RFC 6352) mit Delta-Sync über sync-collection (RFC 6578).

Ablauf pro Account:
1. PROPFIND auf die Basis-URL -> current-user-principal ermitteln
2. PROPFIND auf das Principal -> addressbook-home-set ermitteln
3. PROPFIND auf das Addressbook-Home -> tatsächliche Addressbook-Collection finden
4. REPORT sync-collection mit gespeichertem sync-token -> nur Änderungen abrufen
   (leerer sync-token beim allerersten Lauf -> voller initialer Abruf)

iCloud-Tokens sind laut Google/Apple-Doku ca. 29 Tage gültig; läuft ein Token ab,
antwortet der Server mit 403 valid-sync-token, dann fällt der Client automatisch
auf einen vollständigen Re-Sync zurück.
"""
import logging
import time
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger(__name__)

ICLOUD_BASE_URL = "https://contacts.icloud.com/"

NS = {
    "d": "DAV:",
    "card": "urn:ietf:params:xml:ns:carddav",
}


class SyncTokenInvalid(Exception):
    """Wird geworfen, wenn der Server den gespeicherten sync-token nicht mehr akzeptiert."""


class CardDAVClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30):
        self.base_url = base_url
        self.auth = (username, password)
        self.timeout = timeout
        self.session = requests.Session()

    def _request(self, method: str, url: str, body: str, depth: str = "0", max_retries: int = 3):
        headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
        for attempt in range(max_retries + 1):
            resp = self.session.request(
                method, url, data=body, headers=headers, auth=self.auth, timeout=self.timeout,
            )
            if resp.status_code == 403 and "valid-sync-token" in resp.text:
                raise SyncTokenInvalid("sync-token vom Server abgelehnt")
            if resp.status_code == 503 and attempt < max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 60
                logger.warning("503 Service Unavailable — warte %ds (Versuch %d/%d)", wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            if resp.status_code == 503:
                retry_after = resp.headers.get("Retry-After")
                logger.error("503 Service Unavailable — Retry-After: %s, alle %d Versuche aufgebraucht", retry_after or "nicht angegeben", max_retries)
            resp.raise_for_status()
            return ET.fromstring(resp.content)

    def discover_principal(self) -> str:
        body = """<?xml version="1.0" encoding="utf-8" ?>
        <d:propfind xmlns:d="DAV:">
          <d:prop><d:current-user-principal/></d:prop>
        </d:propfind>"""
        root = self._request("PROPFIND", self.base_url, body)
        href = root.find(".//d:current-user-principal/d:href", NS)
        if href is None:
            raise RuntimeError("current-user-principal nicht gefunden")
        return self._absolute(href.text)

    def discover_addressbook_home(self, principal_url: str) -> str:
        body = """<?xml version="1.0" encoding="utf-8" ?>
        <d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
          <d:prop><card:addressbook-home-set/></d:prop>
        </d:propfind>"""
        root = self._request("PROPFIND", principal_url, body)
        href = root.find(".//card:addressbook-home-set/d:href", NS)
        if href is None:
            raise RuntimeError("addressbook-home-set nicht gefunden")
        return self._absolute(href.text)

    def discover_addressbook_collection(self, home_url: str) -> str:
        body = """<?xml version="1.0" encoding="utf-8" ?>
        <d:propfind xmlns:d="DAV:">
          <d:prop><d:resourcetype/><d:displayname/></d:prop>
        </d:propfind>"""
        root = self._request("PROPFIND", home_url, body, depth="1")
        for response in root.findall("d:response", NS):
            resourcetype = response.find(".//d:resourcetype", NS)
            if resourcetype is not None and any(child.tag.endswith("addressbook") for child in resourcetype):
                href = response.find("d:href", NS)
                if href is not None:
                    return self._absolute(href.text)
        raise RuntimeError("Keine addressbook-Collection gefunden")

    def sync_collection(self, collection_url: str, sync_token: str | None, fetch_missing: bool = True):
        """
        Führt REPORT sync-collection aus.
        Gibt (changed_vcards, etags, deleted_hrefs, new_sync_token) zurück.
        fetch_missing: Wenn True, werden Kontakte ohne address-data per GET nachgeholt.
        """
        token_element = f"<d:sync-token>{sync_token}</d:sync-token>" if sync_token else "<d:sync-token/>"
        body = f"""<?xml version="1.0" encoding="utf-8" ?>
        <d:sync-collection xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
          {token_element}
          <d:sync-level>1</d:sync-level>
          <d:prop><d:getetag/><card:address-data/></d:prop>
        </d:sync-collection>"""
        root = self._request("REPORT", collection_url, body, depth="1")

        responses = root.findall("d:response", NS)
        logger.debug("sync-collection: %d response-Elemente erhalten", len(responses))

        vcards, etags, deleted_hrefs = [], [], []
        missing_hrefs = []
        for response in responses:
            status_el = response.find(".//d:status", NS)
            status_text = status_el.text if status_el is not None else ""
            href_el = response.find("d:href", NS)
            href = href_el.text if href_el is not None else None

            if "404" in status_text:
                logger.debug("  DELETE href=%s status=%s", href, status_text)
                if href:
                    deleted_hrefs.append(href)
                continue

            etag_el = response.find("d:getetag", NS)
            etag = etag_el.text if etag_el is not None else None

            data = response.find(".//card:address-data", NS)
            has_data = data is not None and bool(data.text)
            if has_data:
                logger.debug("  UPSERT href=%s etag=%s (inline data)", href, etag)
                vcards.append(data.text)
                etags.append(etag)
            elif fetch_missing:
                logger.debug("  UPSERT href=%s etag=%s (keine Daten, hole per GET)", href, etag)
                if href:
                    missing_hrefs.append((href, etag))
            else:
                logger.debug("  SKIP href=%s etag=%s (keine Daten, fetch_missing=False)", href, etag)

        for href, etag in missing_hrefs:
            vcard_text = self._get_vcard_by_href(collection_url, href)
            if vcard_text:
                vcards.append(vcard_text)
                etags.append(etag)
            else:
                logger.warning("Konnte vCard nicht abrufen: %s", href)

        new_token_el = root.find("d:sync-token", NS)
        new_token = new_token_el.text if new_token_el is not None else None
        logger.debug("sync-collection Ergebnis: %d vcards, %d deleted, new_token=%s", len(vcards), len(deleted_hrefs), new_token[:20] if new_token else None)
        return vcards, etags, deleted_hrefs, new_token

    def fetch_all_vcards(self, collection_url: str) -> tuple[list[str], list[str | None]]:
        """Fallback für den allerersten, vollen Abruf über addressbook-query.
        Gibt (vcards, etags) zurück."""
        body = """<?xml version="1.0" encoding="utf-8" ?>
        <card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
          <d:prop><d:getetag/><card:address-data/></d:prop>
          <card:filter/>
        </card:addressbook-query>"""
        root = self._request("REPORT", collection_url, body, depth="1")
        vcards, etags = [], []
        for response in root.findall("d:response", NS):
            etag_el = response.find("d:getetag", NS)
            etag = etag_el.text if etag_el is not None else None
            data = response.find(".//card:address-data", NS)
            if data is not None and data.text:
                vcards.append(data.text)
                etags.append(etag)
        return vcards, etags

    def _get_vcard_by_href(self, collection_url: str, href: str) -> str | None:
        url = urljoin(collection_url, href)
        try:
            resp = self.session.get(url, auth=self.auth, timeout=self.timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            logger.warning("GET %s fehlgeschlagen: %s", url, exc)
            return None

    def discover_collection(self) -> str:
        principal = self.discover_principal()
        home = self.discover_addressbook_home(principal)
        collection = self.discover_addressbook_collection(home)
        logger.info("Addressbook-Collection gefunden: %s", collection)
        return collection

    def _absolute(self, href: str) -> str:
        if href.startswith("http"):
            return href
        return urljoin(self.base_url, href)
