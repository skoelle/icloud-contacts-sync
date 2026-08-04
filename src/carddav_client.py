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
from xml.etree import ElementTree as ET
from urllib.parse import urljoin

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

    def _request(self, method: str, url: str, body: str, depth: str = "0"):
        headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
        resp = self.session.request(
            method, url, data=body, headers=headers, auth=self.auth, timeout=self.timeout,
        )
        if resp.status_code == 403 and "valid-sync-token" in resp.text:
            raise SyncTokenInvalid("sync-token vom Server abgelehnt")
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

    def sync_collection(self, collection_url: str, sync_token: str | None):
        """
        Führt REPORT sync-collection aus. Gibt (changed_or_new_vcards, deleted_hrefs, new_sync_token) zurück.
        changed_or_new_vcards: list[str] roher vCard-Text
        deleted_hrefs: list[str] Hrefs von gelöschten Kontakten (status 404)
        """
        token_element = f"<d:sync-token>{sync_token}</d:sync-token>" if sync_token else "<d:sync-token/>"
        body = f"""<?xml version="1.0" encoding="utf-8" ?>
        <d:sync-collection xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
          {token_element}
          <d:sync-level>1</d:sync-level>
          <d:prop><d:getetag/><card:address-data/></d:prop>
        </d:sync-collection>"""
        root = self._request("REPORT", collection_url, body, depth="1")

        vcards, deleted_hrefs = [], []
        for response in root.findall("d:response", NS):
            status_el = response.find(".//d:status", NS)
            status_text = status_el.text if status_el is not None else ""
            href_el = response.find("d:href", NS)
            href = href_el.text if href_el is not None else None

            if "404" in status_text:
                if href:
                    deleted_hrefs.append(href)
                continue

            data = response.find(".//card:address-data", NS)
            if data is not None and data.text:
                vcards.append(data.text)

        new_token_el = root.find("d:sync-token", NS)
        new_token = new_token_el.text if new_token_el is not None else None
        return vcards, deleted_hrefs, new_token

    def fetch_all_vcards(self, collection_url: str) -> list[str]:
        """Fallback für den allerersten, vollen Abruf über addressbook-query."""
        body = """<?xml version="1.0" encoding="utf-8" ?>
        <card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
          <d:prop><d:getetag/><card:address-data/></d:prop>
          <card:filter/>
        </card:addressbook-query>"""
        root = self._request("REPORT", collection_url, body, depth="1")
        vcards = []
        for response in root.findall("d:response", NS):
            data = response.find(".//card:address-data", NS)
            if data is not None and data.text:
                vcards.append(data.text)
        return vcards

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
