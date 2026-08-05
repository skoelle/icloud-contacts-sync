# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
import os
import json
from dotenv import load_dotenv

load_dotenv()

ICLOUD_BASE_URL = "https://contacts.icloud.com/"


class Account:
    def __init__(self, name: str, apple_email: str, apple_app_password: str, authelia_user: str | None):
        self.name = name
        self.apple_email = apple_email
        self.apple_app_password = apple_app_password
        self.authelia_user = authelia_user


class Config:
    MARIADB_HOST = os.environ.get("MARIADB_HOST", "mariadb.internal")
    MARIADB_PORT = int(os.environ.get("MARIADB_PORT", "3306"))
    MARIADB_DATABASE = os.environ.get("MARIADB_DATABASE", "contacts")
    MARIADB_USER = os.environ.get("MARIADB_USER", "")
    MARIADB_PASSWORD = os.environ.get("MARIADB_PASSWORD", "")

    ACCOUNTS_CONFIG_PATH = os.environ.get("ACCOUNTS_CONFIG_PATH", "/app/config/accounts.json")

    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    SOURCE_NAME = "icloud"

    MAILER_ENABLED = os.environ.get("MAILER_ENABLED", "false").lower() == "true"
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
    MAIL_FROM = os.environ.get("MAIL_FROM", "")
    MAIL_TO = os.environ.get("MAIL_TO", "")

    # Authelia liefert den eingeloggten Benutzer per Header, der vom
    # vorgeschalteten nginx/traefik als Remote-User weitergereicht wird.
    AUTH_REMOTE_USER_HEADER = os.environ.get("AUTH_REMOTE_USER_HEADER", "Remote-User")

    API_HOST = os.environ.get("API_HOST", "0.0.0.0")
    API_PORT = int(os.environ.get("API_PORT", "8000"))
    WEB_URL = os.environ.get("WEB_URL", "")

    @classmethod
    def validate_db(cls):
        missing = [n for n, v in [
            ("MARIADB_USER", cls.MARIADB_USER),
            ("MARIADB_PASSWORD", cls.MARIADB_PASSWORD),
        ] if not v]
        if missing:
            raise RuntimeError(f"Fehlende Umgebungsvariablen: {', '.join(missing)}")

    @classmethod
    def validate_mailer(cls):
        missing = [n for n, v in [
            ("SMTP_HOST", cls.SMTP_HOST),
            ("MAIL_FROM", cls.MAIL_FROM),
            ("MAIL_TO", cls.MAIL_TO),
        ] if not v]
        if missing:
            raise RuntimeError(f"Fehlende Mailer-Umgebungsvariablen: {', '.join(missing)}")

    @classmethod
    def _load_raw_accounts_config(cls) -> dict:
        if not os.path.exists(cls.ACCOUNTS_CONFIG_PATH):
            raise RuntimeError(
                f"Accounts-Konfiguration nicht gefunden: {cls.ACCOUNTS_CONFIG_PATH}. "
                f"Kopiere config/accounts.json.example nach config/accounts.json."
            )
        with open(cls.ACCOUNTS_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    @classmethod
    def load_accounts(cls) -> list[Account]:
        data = cls._load_raw_accounts_config()
        accounts_raw = data.get("accounts", [])
        if not accounts_raw:
            raise RuntimeError("accounts.json enthält keine Accounts")

        seen_names = set()
        accounts = []
        for entry in accounts_raw:
            name = entry.get("name")
            email = entry.get("apple_email")
            pwd = entry.get("apple_app_password")
            authelia_user = entry.get("authelia_user")
            if not all([name, email, pwd]):
                raise RuntimeError(f"Unvollständiger Account-Eintrag: {entry}")
            if name in seen_names:
                raise RuntimeError(f"Account-Name '{name}' ist nicht eindeutig")
            seen_names.add(name)
            accounts.append(Account(name, email, pwd, authelia_user))
        return accounts

    @classmethod
    def load_admin_users(cls) -> set[str]:
        data = cls._load_raw_accounts_config()
        return set(data.get("admins", []))

    @classmethod
    def load_authelia_user_map(cls) -> dict[str, str]:
        """Gibt {authelia_user: account_name} zurück, genutzt von der Web-Ansicht/API."""
        accounts = cls.load_accounts()
        mapping = {}
        for acc in accounts:
            if acc.authelia_user:
                if acc.authelia_user in mapping:
                    raise RuntimeError(f"authelia_user '{acc.authelia_user}' ist mehreren Accounts zugeordnet")
                mapping[acc.authelia_user] = acc.name
        return mapping
