#!/usr/bin/env python3
"""Lokale Demo-App: SQLite-Backend mit Fake-Kontakten für Dashboard + Kontakt-Detail."""
import json
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from utils import fmt_birthday_age, fmt_birthday_short, is_unknown_year

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("demo")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "demo.db"
SRC_DIR = BASE_DIR / "src"

app = FastAPI(title="iCloud Contacts Sync – Demo", version="1.0.0")
app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(32), session_cookie="ics_session")
app.mount("/static", StaticFiles(directory=str(SRC_DIR / "api" / "static")), name="static")
templates = Jinja2Templates(directory=str(SRC_DIR / "api" / "templates"))
templates.env.filters["urlquote"] = lambda s: quote_plus(s or "")
templates.env.filters["fmt_birthday"] = fmt_birthday_short
templates.env.filters["fmt_age"] = fmt_birthday_age
templates.env.filters["has_year"] = lambda b: not is_unknown_year(b)

DEMO_USER = "demo"
DEMO_ACCOUNT = "demo-user"


def _fmt_ts(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    except (ValueError, TypeError):
        return ts


@contextmanager
def get_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def _build_full_name(row: dict) -> str | None:
    parts = [
        row.get("given_name"),
        row.get("middle_name"),
        row.get("family_name"),
    ]
    return " ".join(p for p in parts if p) or None


def _row_to_contact_out(row: dict) -> dict:
    row = dict(row)
    for field in ("emails", "phones", "addresses", "urls", "social_profiles", "categories"):
        raw = row.get(field)
        row[field] = json.loads(raw) if raw else []
    if row.get("birthday") and isinstance(row["birthday"], str):
        row["birthday"] = date.fromisoformat(row["birthday"])
    if not row.get("full_name"):
        row["full_name"] = _build_full_name(row)
    return row


def init_db():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account TEXT NOT NULL,
                uid TEXT NOT NULL,
                full_name TEXT, given_name TEXT, family_name TEXT,
                middle_name TEXT, prefix TEXT, suffix TEXT,
                organization TEXT, job_title TEXT,
                birthday TEXT, notes TEXT,
                emails TEXT, phones TEXT, addresses TEXT,
                urls TEXT, social_profiles TEXT, categories TEXT,
                photo_url TEXT,
                raw_vcard TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(account, uid)
            );
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account TEXT NOT NULL,
                uid TEXT NOT NULL,
                name TEXT,
                UNIQUE(account, uid)
            );
            CREATE TABLE IF NOT EXISTS group_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER REFERENCES groups(id),
                member_uid TEXT NOT NULL,
                UNIQUE(group_id, member_uid)
            );
            CREATE TABLE IF NOT EXISTS sync_runs (
                id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                sync_type TEXT NOT NULL DEFAULT 'delta',
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                contacts_upserted INTEGER,
                contacts_deleted INTEGER,
                error_message TEXT
            );
        """)


def seed_data():
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        if count > 0:
            return

        today = date.today()
        def _avatar(name: str, bg: str = "0066cc") -> str:
            from urllib.parse import quote
            return f"https://ui-avatars.com/api/?name={quote(name)}&background={bg}&color=fff&size=160&bold=true"

        contacts = [
            {
                "uid": "demo-001", "full_name": "Erika Musterfrau",
                "given_name": "Erika", "family_name": "Musterfrau",
                "organization": "Muster GmbH", "job_title": "Geschäftsführerin",
                "birthday": "1985-03-15",
                "photo_url": _avatar("Erika Musterfrau", "0066cc"),
                "emails": json.dumps([{"type": "work", "value": "erika@muster.de"}, {"type": "home", "value": "erika.privat@mail.de"}]),
                "phones": json.dumps([{"type": "work", "value": "+49 30 1234567"}, {"type": "mobile", "value": "+49 170 1234567"}]),
                "addresses": json.dumps([{"type": "work", "street": "Berliner Str. 1", "zip": "10115", "city": "Berlin", "region": "Berlin", "country": "Deutschland"}]),
            },
            {
                "uid": "demo-002", "full_name": "Thomas Testermann",
                "given_name": "Thomas", "family_name": "Testermann",
                "organization": "Test AG", "job_title": "Entwickler",
                "birthday": "1990-07-22",
                "photo_url": _avatar("Thomas Testermann", "28a745"),
                "emails": json.dumps([{"type": "work", "value": "thomas@test.de"}]),
                "phones": json.dumps([{"type": "mobile", "value": "+49 171 9876543"}]),
                "addresses": json.dumps([{"type": "home", "street": "Musterweg 5", "zip": "80331", "city": "München", "region": "Bayern", "country": "Deutschland"}]),
            },
            {
                "uid": "demo-003", "full_name": "Anna Beispiel",
                "given_name": "Anna", "family_name": "Beispiel",
                "organization": "Beispiel & Partner", "job_title": "Designerin",
                "birthday": "1992-11-08",
                "photo_url": _avatar("Anna Beispiel", "dc3545"),
                "emails": json.dumps([{"type": "home", "value": "anna@beispiel.de"}]),
                "phones": json.dumps([{"type": "mobile", "value": "+49 172 5551234"}]),
                "addresses": json.dumps([{"type": "home", "street": "Gartenstr. 12", "zip": "50667", "city": "Köln", "region": "Nordrhein-Westfalen", "country": "Deutschland"}]),
            },
            {
                "uid": "demo-004", "full_name": "Sabine Mustermann",
                "given_name": "Sabine", "family_name": "Mustermann",
                "organization": "", "job_title": "",
                "birthday": f"1997-08-{(today + timedelta(days=5)).day:02d}",
                "photo_url": _avatar("Sabine Mustermann", "fd7e14"),
                "emails": json.dumps([{"type": "home", "value": "sabine@web.de"}]),
                "phones": json.dumps([{"type": "home", "value": "+49 721 555999"}]),
                "addresses": json.dumps([{"type": "home", "street": "Waldweg 3", "zip": "70173", "city": "Stuttgart", "region": "Baden-Württemberg", "country": "Deutschland"}]),
            },
            {
                "uid": "demo-005", "full_name": "Peter Beispiel",
                "given_name": "Peter", "family_name": "Beispiel",
                "organization": "Beispiel & Partner", "job_title": "Partner",
                "birthday": "1960-05-01",
                "photo_url": _avatar("Peter Beispiel", "6f42c1"),
                "emails": json.dumps([{"type": "work", "value": "peter@beispiel.de"}]),
                "phones": json.dumps([{"type": "work", "value": "+49 721 555888"}, {"type": "mobile", "value": "+49 175 1112233"}]),
                "addresses": json.dumps([{"type": "work", "street": "Hauptstr. 42", "zip": "70173", "city": "Stuttgart", "region": "Baden-Württemberg", "country": "Deutschland"}]),
            },
            {
                "uid": "demo-006", "full_name": "Max Demo",
                "given_name": "Max", "family_name": "Demo",
                "organization": "Demo Inc.", "job_title": "Projektmanager",
                "birthday": f"1991-08-{(today + timedelta(days=3)).day:02d}",
                "photo_url": _avatar("Max Demo", "17a2b8"),
                "emails": json.dumps([{"type": "work", "value": "max@demo.de"}, {"type": "home", "value": "max.privat@demo.de"}]),
                "phones": json.dumps([{"type": "mobile", "value": "+49 176 4445566"}]),
                "addresses": json.dumps([{"type": "home", "street": "Demoallee 7", "zip": "10115", "city": "Berlin", "region": "Berlin", "country": "Deutschland"}]),
            },
        ]

        for c in contacts:
            conn.execute(
                """INSERT INTO contacts (account, uid, full_name, given_name, family_name,
                   organization, job_title, birthday, photo_url, emails, phones, addresses)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (DEMO_ACCOUNT, c["uid"], c["full_name"], c["given_name"], c["family_name"],
                 c["organization"], c["job_title"], c["birthday"], c["photo_url"],
                 c["emails"], c["phones"], c["addresses"]),
            )

        groups = [
            ("demo-grp-001", "Familie"),
            ("demo-grp-002", "Arbeit"),
        ]
        for uid, name in groups:
            conn.execute(
                "INSERT INTO groups (account, uid, name) VALUES (?, ?, ?)",
                (DEMO_ACCOUNT, uid, name),
            )

        family_id = conn.execute("SELECT id FROM groups WHERE uid = ?", ("demo-grp-001",)).fetchone()[0]
        work_id = conn.execute("SELECT id FROM groups WHERE uid = ?", ("demo-grp-002",)).fetchone()[0]

        for uid in ["demo-001", "demo-004", "demo-005"]:
            conn.execute("INSERT INTO group_members (group_id, member_uid) VALUES (?, ?)", (family_id, uid))
        for uid in ["demo-001", "demo-002", "demo-003", "demo-005"]:
            conn.execute("INSERT INTO group_members (group_id, member_uid) VALUES (?, ?)", (work_id, uid))

        conn.execute(
            """INSERT INTO sync_runs (id, account, sync_type, started_at, finished_at, status, contacts_upserted, contacts_deleted)
               VALUES (?, ?, ?, datetime('now', '-2 hours'), datetime('now', '-1 hour'), 'success', 6, 0)""",
            ("demo-sync-001", DEMO_ACCOUNT, "delta"),
        )

        conn.commit()
        logger.info("Demo-Daten erstellt: 6 Kontakte, 2 Gruppen")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with get_connection() as conn:
        contact_count = conn.execute("SELECT COUNT(*) AS total FROM contacts").fetchone()["total"]

        today = date.today()
        rows = conn.execute(
            """SELECT id, full_name, given_name, middle_name, family_name,
                      organization, birthday, photo_url
               FROM contacts
               WHERE birthday IS NOT NULL
               ORDER BY birthday""",
        ).fetchall()
        upcoming = []
        cutoff = today + timedelta(days=7)
        for r in rows:
            bday = date.fromisoformat(r["birthday"])
            bday_this_year = bday.replace(year=today.year)
            if today <= bday_this_year <= cutoff:
                d = dict(r)
                d["birthday"] = bday
                if not d.get("full_name"):
                    d["full_name"] = _build_full_name(d)
                upcoming.append(d)

        groups = [dict(g) for g in conn.execute(
            "SELECT id, name, uid FROM groups ORDER BY name"
        ).fetchall()]

        last_sync_row = conn.execute(
            """SELECT id, sync_type, started_at, finished_at, status,
                      contacts_upserted, contacts_deleted, error_message
               FROM sync_runs ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
        last_sync = dict(last_sync_row) if last_sync_row else None
        if last_sync:
            last_sync["started_at"] = _fmt_ts(last_sync["started_at"])
            last_sync["finished_at"] = _fmt_ts(last_sync["finished_at"])

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_user": DEMO_USER,
            "is_admin": True,
            "show_all": False,
            "account_name": DEMO_ACCOUNT,
            "contact_count": contact_count,
            "upcoming_birthdays": upcoming,
            "last_sync": last_sync,
            "last_sync_with_changes": None,
            "groups": groups,
            "current_year": today.year,
            "today": today,
        },
    )


@app.get("/contacts/{contact_id}", response_class=HTMLResponse)
def contact_detail(request: Request, contact_id: int):
    with get_connection() as conn:
        row = conn.execute(
            """SELECT id, account, uid, full_name, given_name, middle_name, family_name,
                      organization, job_title, birthday, notes, photo_url,
                      emails, phones, addresses, urls, social_profiles, categories, updated_at
               FROM contacts WHERE id = ?""",
            (contact_id,),
        ).fetchone()

        if not row:
            return HTMLResponse("Kontakt nicht gefunden", status_code=404)

        contact = _row_to_contact_out(row)

        groups = [dict(g) for g in conn.execute(
            """SELECT g.id, g.name, g.uid
               FROM groups g
               JOIN group_members gm ON gm.group_id = g.id
               WHERE gm.member_uid = ?
               ORDER BY g.name""",
            (row["uid"],),
        ).fetchall()]

    return templates.TemplateResponse(
        "contact.html",
        {
            "request": request,
            "current_user": DEMO_USER,
            "is_admin": True,
            "show_all": False,
            "contact": contact,
            "groups": groups,
            "search": "",
            "custom_links": [],
        },
    )


@app.get("/search", response_class=HTMLResponse)
def search_redirect():
    return HTMLResponse(
        '<!DOCTYPE html><html><body style="font-family:sans-serif;padding:2rem">'
        '<h2>Demo-Modus</h2><p><a href="/">← Zurück zum Dashboard</a></p>'
        '</body></html>',
        status_code=302,
        headers={"Location": "/"},
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "demo"}


if __name__ == "__main__":
    import uvicorn
    init_db()
    seed_data()
    logger.info("Starte Demo-Server auf http://127.0.0.1:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
