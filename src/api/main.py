# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""FastAPI-App für die interne Web-Ansicht/API der iCloud-Kontakte.

Läuft im selben Image wie der Sync-Container, wird aber über einen
eigenen Docker-Compose-Service mit abweichendem Startbefehl gestartet
(uvicorn statt sync/mailer). Zugriff ausschließlich über einen
vorgeschalteten Reverse-Proxy mit Authelia, der den eingeloggten
Benutzernamen im Remote-User-Header mitschickt."""
import json
import logging
from datetime import date

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
from api.auth import get_current_user, resolve_account_for_user
from api.schemas import ContactListResponse, ContactOut, SyncRunOut
from config import Config

logging.basicConfig(level=Config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api")

app = FastAPI(title="iCloud Contacts Sync – Interne API", version="1.0.0")
app.mount("/static", StaticFiles(directory="api/static"), name="static")
templates = Jinja2Templates(directory="api/templates")


def _row_to_contact_out(row: dict) -> dict:
    row = dict(row)
    for field in ["emails", "phones", "addresses", "urls", "categories"]:
        raw = row.get(field)
        row[field] = json.loads(raw) if raw else []
    row["updated_at"] = str(row["updated_at"])
    return row


def _account_filter_clause(account_name: str | None) -> tuple[str, list]:
    if account_name is None:
        return "", []
    return "WHERE account = %s", [account_name]


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/contacts", response_model=ContactListResponse)
def list_contacts(
    request: Request,
    q: str | None = Query(default=None, description="Freitextsuche über Name, Organisation, E-Mail"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: str = Depends(get_current_user),
):
    account_name, is_admin = resolve_account_for_user(current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        search_clause = ""
        if q:
            search_op = "AND" if where_clause else "WHERE"
            search_clause = f" {search_op} (full_name LIKE %s OR organization LIKE %s OR emails LIKE %s)"
            like = f"%{q}%"
            params.extend([like, like, like])

        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM contacts {where_clause}{search_clause}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""SELECT id, account, uid, full_name, given_name, family_name, organization,
                           job_title, birthday, notes, emails, phones, addresses, urls, categories, updated_at
                    FROM contacts {where_clause}{search_clause}
                    ORDER BY full_name
                    LIMIT %s OFFSET %s""",
                params + [limit, offset],
            )
            rows = cur.fetchall()

    items = [_row_to_contact_out(r) for r in rows]
    return {"total": total, "items": items}


@app.get("/api/contacts/{contact_id}", response_model=ContactOut)
def get_contact(contact_id: int, current_user: str = Depends(get_current_user)):
    account_name, is_admin = resolve_account_for_user(current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        id_clause = "AND id = %s" if where_clause else "WHERE id = %s"
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, account, uid, full_name, given_name, family_name, organization,
                           job_title, birthday, notes, emails, phones, addresses, urls, categories, updated_at
                    FROM contacts {where_clause} {id_clause}""",
                params + [contact_id],
            )
            row = cur.fetchone()

    if not row:
        return {}
    return _row_to_contact_out(row)


@app.get("/api/contacts/birthdays/today", response_model=list[ContactOut])
def birthdays_today(current_user: str = Depends(get_current_user)):
    account_name, is_admin = resolve_account_for_user(current_user)
    today = date.today()

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        month_day_clause = "AND MONTH(birthday) = %s AND DAY(birthday) = %s" if where_clause \
            else "WHERE MONTH(birthday) = %s AND DAY(birthday) = %s"
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, account, uid, full_name, given_name, family_name, organization,
                           job_title, birthday, notes, emails, phones, addresses, urls, categories, updated_at
                    FROM contacts {where_clause} {month_day_clause}
                    ORDER BY full_name""",
                params + [today.month, today.day],
            )
            rows = cur.fetchall()

    return [_row_to_contact_out(r) for r in rows]


@app.get("/api/contacts/birthdays/upcoming")
def birthdays_upcoming(
    days: int = Query(default=7, ge=1, le=90),
    current_user: str = Depends(get_current_user),
):
    account_name, is_admin = resolve_account_for_user(current_user)
    with db.get_connection() as conn:
        rows = db.get_upcoming_birthdays(conn, account_name, days)
    return {"days": days, "items": rows}


@app.get("/api/contacts/count")
def contact_count(current_user: str = Depends(get_current_user)):
    account_name, is_admin = resolve_account_for_user(current_user)
    with db.get_connection() as conn:
        total = db.get_contact_count(conn, account_name)
    return {"total": total}


@app.get("/api/sync-runs", response_model=list[SyncRunOut])
def list_sync_runs(current_user: str = Depends(get_current_user)):
    account_name, is_admin = resolve_account_for_user(current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, account, sync_type, started_at, finished_at, status,
                           contacts_upserted, contacts_deleted, error_message
                    FROM sync_runs {where_clause}
                    ORDER BY started_at DESC
                    LIMIT 50""",
                params,
            )
            rows = cur.fetchall()

    for r in rows:
        r["started_at"] = str(r["started_at"])
        r["finished_at"] = str(r["finished_at"]) if r["finished_at"] else None
    return rows


@app.get("/", response_class=HTMLResponse)
def web_dashboard(
    request: Request,
    current_user: str = Depends(get_current_user),
):
    account_name, is_admin = resolve_account_for_user(current_user)

    with db.get_connection() as conn:
        contact_count = db.get_contact_count(conn, account_name)
        upcoming_birthdays = db.get_upcoming_birthdays(conn, account_name, 7)
        where_clause, params = _account_filter_clause(account_name)
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, account, sync_type, started_at, finished_at, status,
                           contacts_upserted, contacts_deleted, error_message
                    FROM sync_runs {where_clause}
                    ORDER BY started_at DESC
                    LIMIT 1""",
                params,
            )
            last_sync = cur.fetchone()

    if last_sync:
        last_sync["started_at"] = str(last_sync["started_at"])
        last_sync["finished_at"] = str(last_sync["finished_at"]) if last_sync["finished_at"] else None

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_user": current_user,
            "is_admin": is_admin,
            "account_name": account_name or "alle Accounts",
            "contact_count": contact_count,
            "upcoming_birthdays": upcoming_birthdays,
            "last_sync": last_sync,
            "current_year": date.today().year,
        },
    )


@app.get("/search", response_class=HTMLResponse)
def web_search(
    request: Request,
    search: str | None = Query(default=None),
    current_user: str = Depends(get_current_user),
):
    account_name, is_admin = resolve_account_for_user(current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        search_clause = ""
        if search:
            search_op = "AND" if where_clause else "WHERE"
            search_clause = f" {search_op} (full_name LIKE %s OR organization LIKE %s)"
            like = f"%{search}%"
            params.extend([like, like])

        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, full_name, organization, birthday, account
                    FROM contacts {where_clause}{search_clause}
                    ORDER BY full_name
                    LIMIT 200""",
                params,
            )
            rows = cur.fetchall()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "current_user": current_user,
            "is_admin": is_admin,
            "account_name": account_name or "alle Accounts",
            "contacts": rows,
            "search": search or "",
        },
    )


@app.get("/contacts/{contact_id}", response_class=HTMLResponse)
def web_contact(
    request: Request,
    contact_id: int,
    search: str | None = Query(default=None),
    current_user: str = Depends(get_current_user),
):
    account_name, is_admin = resolve_account_for_user(current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        id_clause = "AND id = %s" if where_clause else "WHERE id = %s"
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, account, uid, full_name, given_name, family_name, organization,
                           job_title, birthday, notes, emails, phones, addresses, urls, categories, updated_at
                    FROM contacts {where_clause} {id_clause}""",
                params + [contact_id],
            )
            row = cur.fetchone()

    if not row:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/search", status_code=303)

    contact = _row_to_contact_out(row)

    return templates.TemplateResponse(
        "contact.html",
        {
            "request": request,
            "current_user": current_user,
            "is_admin": is_admin,
            "contact": contact,
            "search": search or "",
        },
    )
