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
import secrets
from datetime import date, datetime
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import db
from api.auth import get_current_user, resolve_account_for_user
from api.schemas import (
    ContactListResponse,
    ContactOut,
    GroupDetailOut,
    GroupListResponse,
    SyncRunOut,
)
from config import Config
from mailer import build_message, fetch_birthdays_for_date, send_message
from utils import fmt_birthday_age, fmt_birthday_short, is_unknown_year

logging.basicConfig(level=Config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api")

app = FastAPI(title="iCloud Contacts Sync – Interne API", version="1.0.0")
app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(32), session_cookie="ics_session")
app.mount("/static", StaticFiles(directory="api/static"), name="static")
templates = Jinja2Templates(directory="api/templates")
templates.env.filters["urlquote"] = lambda s: quote_plus(s or "")
templates.env.filters["fmt_birthday"] = fmt_birthday_short
templates.env.filters["fmt_age"] = fmt_birthday_age
templates.env.filters["has_year"] = lambda b: not is_unknown_year(b)


def _fmt_ts(dt) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(Config.TIMEZONE).strftime("%d.%m.%Y %H:%M:%S")


def _row_to_contact_out(row: dict, group_names: list[str] | None = None) -> dict:
    row = dict(row)
    for field in ["emails", "phones", "addresses", "urls", "social_profiles", "categories"]:
        raw = row.get(field)
        row[field] = json.loads(raw) if raw else []
    if not row.get("full_name"):
        row["full_name"] = db._build_full_name(row)
    row["updated_at"] = _fmt_ts(row["updated_at"])
    row["groups"] = group_names if group_names is not None else []
    return row


def _account_filter_clause(account_name: str | None) -> tuple[str, list]:
    if account_name is None:
        return "", []
    return "WHERE account = %s", [account_name]


def _resolve_effective_account(request: Request, current_user: str) -> tuple[str | None, bool, bool]:
    account_name, is_admin = resolve_account_for_user(current_user)
    if not is_admin:
        return account_name, False, False
    show_all = request.session.get("show_all", False)
    if show_all:
        return None, True, True
    return account_name, True, False


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
            search_clause = f" {search_op} (full_name LIKE %s OR given_name LIKE %s OR family_name LIKE %s OR organization LIKE %s OR emails LIKE %s)"
            like = f"%{q}%"
            params.extend([like, like, like, like, like])

        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM contacts {where_clause}{search_clause}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""SELECT id, account, uid, full_name, given_name, family_name, organization,
                           job_title, birthday, notes, photo_url, emails, phones, addresses, urls, social_profiles, categories, updated_at
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
                           job_title, birthday, notes, photo_url, emails, phones, addresses, urls, social_profiles, categories, updated_at
                    FROM contacts {where_clause} {id_clause}""",
                params + [contact_id],
            )
            row = cur.fetchone()

        if not row:
            return {}

        groups = db.get_groups_for_contact(conn, row["account"], row["uid"])
        group_names = [g["name"] for g in groups if g.get("name")]

    return _row_to_contact_out(row, group_names=group_names)


@app.get("/api/contacts/birthdays/today", response_model=list[ContactOut])
def birthdays_today(current_user: str = Depends(get_current_user)):
    account_name, is_admin = resolve_account_for_user(current_user)
    today = datetime.now(Config.TIMEZONE).date()

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        month_day_clause = "AND MONTH(birthday) = %s AND DAY(birthday) = %s" if where_clause \
            else "WHERE MONTH(birthday) = %s AND DAY(birthday) = %s"
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, account, uid, full_name, given_name, family_name, organization,
                           job_title, birthday, notes, photo_url, emails, phones, addresses, urls, social_profiles, categories, updated_at
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
        r["started_at"] = _fmt_ts(r["started_at"])
        r["finished_at"] = _fmt_ts(r["finished_at"])
    return rows


@app.get("/api/groups", response_model=GroupListResponse)
def list_groups(
    request: Request,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: str = Depends(get_current_user),
):
    account_name, is_admin = resolve_account_for_user(current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM `groups` {where_clause}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""SELECT g.id, g.account, g.uid, g.name, g.updated_at,
                           (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) AS member_count
                    FROM `groups` g {where_clause}
                    ORDER BY g.name
                    LIMIT %s OFFSET %s""",
                params + [limit, offset],
            )
            rows = cur.fetchall()

    for r in rows:
        r["updated_at"] = _fmt_ts(r["updated_at"])
    return {"total": total, "items": rows}


@app.get("/api/groups/{group_id}", response_model=GroupDetailOut)
def get_group(group_id: int, current_user: str = Depends(get_current_user)):
    account_name, is_admin = resolve_account_for_user(current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        id_clause = "AND g.id = %s" if where_clause else "WHERE g.id = %s"
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT g.id, g.account, g.uid, g.name, g.updated_at,
                           (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) AS member_count
                    FROM `groups` g {where_clause} {id_clause}""",
                params + [group_id],
            )
            group_row = cur.fetchone()

            if not group_row:
                return {}

            cur.execute(
                """SELECT gm.member_uid, c.id, c.full_name, c.given_name, c.family_name
                   FROM group_members gm
                   LEFT JOIN contacts c ON c.account = g.account AND c.uid = gm.member_uid
                   CROSS JOIN `groups` g
                   WHERE g.id = %s AND gm.group_id = g.id""",
                (group_id,),
            )
            members = cur.fetchall()

    for m in members:
        if not m.get("full_name"):
            m["full_name"] = db._build_full_name(m) if any(m.get(k) for k in ("given_name", "family_name")) else None

    group_row["updated_at"] = _fmt_ts(group_row["updated_at"])
    group_row["members"] = [
        {"member_uid": m["member_uid"], "full_name": m["full_name"], "id": m["id"]}
        for m in members
    ]
    return group_row


@app.get("/api/groups/{group_id}/members")
def get_group_members(group_id: int, current_user: str = Depends(get_current_user)):
    account_name, is_admin = resolve_account_for_user(current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT g.id FROM `groups` g {where_clause}
                    {"AND" if where_clause else "WHERE"} g.id = %s""",
                params + [group_id],
            )
            if not cur.fetchone():
                return {}

            cur.execute(
                """SELECT gm.member_uid, c.id, c.full_name, c.given_name, c.family_name,
                          c.organization, c.birthday, c.photo_url
                   FROM group_members gm
                   LEFT JOIN contacts c ON c.uid = gm.member_uid
                   WHERE gm.group_id = %s""",
                (group_id,),
            )
            rows = cur.fetchall()

    for r in rows:
        if not r.get("full_name"):
            r["full_name"] = db._build_full_name(r) if any(r.get(k) for k in ("given_name", "family_name")) else None
    return {"group_id": group_id, "members": rows}


@app.get("/", response_class=HTMLResponse)
def web_dashboard(
    request: Request,
    current_user: str = Depends(get_current_user),
):
    account_name, is_admin, show_all = _resolve_effective_account(request, current_user)

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

            change_filter = "AND account = %s" if account_name else ""
            change_params = [account_name] if account_name else []
            cur.execute(
                f"""SELECT started_at, contacts_upserted
                    FROM sync_runs
                    WHERE contacts_upserted > 0 {change_filter}
                    ORDER BY started_at DESC
                    LIMIT 1""",
                change_params,
            )
            last_sync_with_changes = cur.fetchone()

    if last_sync:
        last_sync["started_at"] = _fmt_ts(last_sync["started_at"])
        last_sync["finished_at"] = _fmt_ts(last_sync["finished_at"])

    if last_sync_with_changes:
        last_sync_with_changes["started_at"] = _fmt_ts(last_sync_with_changes["started_at"])
        if last_sync and last_sync["started_at"] == last_sync_with_changes["started_at"]:
            last_sync_with_changes = None

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_user": current_user,
            "is_admin": is_admin,
            "show_all": show_all,
            "account_name": account_name or "alle Accounts",
            "contact_count": contact_count,
            "upcoming_birthdays": upcoming_birthdays,
            "last_sync": last_sync,
            "last_sync_with_changes": last_sync_with_changes,
            "current_year": datetime.now(Config.TIMEZONE).date().year,
            "today": datetime.now(Config.TIMEZONE).date(),
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def web_admin(
    request: Request,
    current_user: str = Depends(get_current_user),
):
    _, is_admin, _ = _resolve_effective_account(request, current_user)
    if not is_admin:
        return RedirectResponse(url="/", status_code=303)

    show_all = request.session.get("show_all", False)
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "current_user": current_user,
            "show_all": show_all,
        },
    )


@app.post("/admin/toggle")
def admin_toggle(
    request: Request,
    current_user: str = Depends(get_current_user),
):
    _, is_admin, _ = _resolve_effective_account(request, current_user)
    if not is_admin:
        return RedirectResponse(url="/", status_code=303)

    show_all = request.session.get("show_all", False)
    request.session["show_all"] = not show_all
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/test-send")
def admin_test_send(
    request: Request,
    current_user: str = Depends(get_current_user),
):
    _, is_admin, _ = _resolve_effective_account(request, current_user)
    if not is_admin:
        return RedirectResponse(url="/", status_code=303)

    target = date(2026, 8, 6)
    try:
        Config.validate_mailer()
    except RuntimeError as exc:
        return templates.TemplateResponse(
            "admin.html",
            {
                "request": request,
                "current_user": current_user,
                "show_all": request.session.get("show_all", False),
                "error": str(exc),
            },
            status_code=400,
        )

    with db.get_connection() as conn:
        birthdays = fetch_birthdays_for_date(conn, target)

    msg = build_message(birthdays, target_date=target)
    try:
        send_message(msg)
    except Exception as exc:
        return templates.TemplateResponse(
            "admin.html",
            {
                "request": request,
                "current_user": current_user,
                "show_all": request.session.get("show_all", False),
                "error": f"Versand fehlgeschlagen: {exc}",
            },
            status_code=500,
        )

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "current_user": current_user,
            "show_all": request.session.get("show_all", False),
            "success": f"Test-Mail für {target.strftime('%d.%m.%Y')} gesendet ({len(birthdays)} Kontakte).",
        },
    )


@app.get("/search/special", response_class=HTMLResponse)
def web_search_special(
    request: Request,
    type: str = Query(...),
    current_user: str = Depends(get_current_user),
):
    account_name, is_admin, show_all = _resolve_effective_account(request, current_user)

    query_fn = {
        "no_photo": db.search_contacts_without_photo,
        "no_city": db.search_contacts_without_city,
        "no_social": db.search_contacts_without_social,
    }.get(type)

    if not query_fn:
        return RedirectResponse(url="/", status_code=303)

    title_map = {
        "no_photo": "Kontakte ohne Bild",
        "no_city": "Kontakte ohne Stadt",
        "no_social": "Kontakte ohne Social Profil",
    }

    with db.get_connection() as conn:
        rows = query_fn(conn, account_name)

    for row in rows:
        if not row.get("full_name"):
            row["full_name"] = db._build_full_name(row)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "current_user": current_user,
            "is_admin": is_admin,
            "show_all": show_all,
            "account_name": account_name or "alle Accounts",
            "contacts": rows,
            "search": "",
            "search_title": title_map.get(type, "Suche"),
        },
    )


@app.get("/search", response_class=HTMLResponse)
def web_search(
    request: Request,
    search: str | None = Query(default=None),
    current_user: str = Depends(get_current_user),
):
    account_name, is_admin, show_all = _resolve_effective_account(request, current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        search_clause = ""
        if search:
            search_op = "AND" if where_clause else "WHERE"
            search_clause = f" {search_op} (full_name LIKE %s OR given_name LIKE %s OR family_name LIKE %s OR organization LIKE %s)"
            like = f"%{search}%"
            params.extend([like, like, like, like])

        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, full_name, given_name, middle_name, family_name,
                           prefix, suffix, organization, birthday, account, photo_url
                    FROM contacts {where_clause}{search_clause}
                    ORDER BY full_name
                    LIMIT 200""",
                params,
            )
            rows = cur.fetchall()

    for row in rows:
        if not row.get("full_name"):
            row["full_name"] = db._build_full_name(row)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "current_user": current_user,
            "is_admin": is_admin,
            "show_all": show_all,
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
    account_name, is_admin, show_all = _resolve_effective_account(request, current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        id_clause = "AND id = %s" if where_clause else "WHERE id = %s"
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, account, uid, full_name, given_name, family_name, organization,
                           job_title, birthday, notes, photo_url, emails, phones, addresses, urls, social_profiles, categories, updated_at
                    FROM contacts {where_clause} {id_clause}""",
                params + [contact_id],
            )
            row = cur.fetchone()

        if not row:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/search", status_code=303)

        groups = db.get_groups_for_contact(conn, row["account"], row["uid"])
        group_names = [g["name"] for g in groups if g.get("name")]

    contact = _row_to_contact_out(row, group_names=group_names)

    homecity = ""
    workcity = ""
    for addr in contact.get("addresses", []):
        city = (addr.get("city") or "").strip()
        if not city:
            continue
        addr_type = (addr.get("type") or "").lower()
        if addr_type == "home" and not homecity:
            homecity = city
        elif addr_type == "work" and not workcity:
            workcity = city

    custom_links = []
    contact_account = contact.get("account")
    if contact_account:
        accounts = Config.load_accounts()
        for acc in accounts:
            if acc.name == contact_account:
                custom_links = acc.custom_links
                break

    resolved_links = []
    for link in custom_links:
        url = link["url"]
        if "[homecity]" in url and not homecity:
            continue
        if "[workcity]" in url and not workcity:
            continue
        url = url.replace("[fullname]", quote_plus(contact.get("full_name") or ""))
        url = url.replace("[homecity]", quote_plus(homecity))
        url = url.replace("[workcity]", quote_plus(workcity))
        resolved_links.append({"label": link["label"], "url": url})

    return templates.TemplateResponse(
        "contact.html",
        {
            "request": request,
            "current_user": current_user,
            "is_admin": is_admin,
            "show_all": show_all,
            "contact": contact,
            "search": search or "",
            "custom_links": resolved_links,
        },
    )
