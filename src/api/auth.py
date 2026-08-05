# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Authelia-Integration: liest den vom Reverse-Proxy weitergereichten
Remote-User-Header und mappt ihn per accounts.json auf einen internen
Account-Namen. Kein eigenes Login, Authelia übernimmt die eigentliche
Authentifizierung vorgeschaltet."""
from fastapi import Header, HTTPException, status

from config import Config


def get_current_user(remote_user: str | None = Header(default=None, alias="Remote-User")) -> str:
    """Extrahiert den Authelia-Benutzernamen aus dem konfigurierten Header.
    Der Header-Name ist über AUTH_REMOTE_USER_HEADER konfigurierbar, FastAPI
    bindet hier auf den Default 'Remote-User', siehe Hinweis in README.md
    falls du einen anderen Header-Namen in Authelia/nginx konfiguriert hast."""
    if not remote_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Kein Remote-User-Header vom Reverse-Proxy erhalten. "
                   "Läuft die Anwendung hinter Authelia mit korrektem auth_request-Setup?",
        )
    return remote_user


def resolve_account_for_user(authelia_user: str) -> tuple[str | None, bool]:
    """Gibt (account_name, is_admin) zurück.
    account_name ist None, wenn der User als Admin auf ALLE Accounts zugreifen darf.
    Ist der User weder gemappt noch Admin, wird eine 403 geworfen."""
    admins = Config.load_admin_users()
    user_map = Config.load_authelia_user_map()

    is_admin = authelia_user in admins

    if is_admin:
        return None, True

    account_name = user_map.get(authelia_user)
    if not account_name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Authelia-Benutzer '{authelia_user}' ist keinem Account in accounts.json zugeordnet.",
        )
    return account_name, False
