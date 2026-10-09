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
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote_plus

import requests
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import db
from api.auth import get_current_user, resolve_account_for_user
from api.schemas import (
    ChatTopResponse,
    ContactListResponse,
    ContactOut,
    GroupDetailOut,
    GroupListResponse,
    SyncRunOut,
)
from config import Config
from mailer import build_message, fetch_todays_birthdays_for_account, send_message
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
    for field in ["emails", "phones", "addresses", "urls", "social_profiles", "related_names", "categories"]:
        raw = row.get(field)
        row[field] = json.loads(raw) if raw else []
    if not row.get("full_name"):
        row["full_name"] = db._build_full_name(row)
    row["updated_at"] = _fmt_ts(row["updated_at"])
    row["groups"] = group_names if group_names is not None else []
    return row


def _enrich_related_names(conn, contact: dict) -> None:
    related = contact.get("related_names", [])
    if not related:
        return
    names = list({r["value"] for r in related if r.get("value")})
    if not names:
        return
    resolved = db.resolve_related_names(conn, contact["account"], names)
    for r in related:
        name = r.get("value", "")
        info = resolved.get(name)
        if info:
            r["id"] = info["id"]
            r["name"] = info["name"]
        else:
            r["id"] = None
            r["name"] = name


def _account_filter_clause(account_name: str | None) -> tuple[str, list]:
    if account_name is None:
        return "", []
    return "WHERE account = %s", [account_name]


CHAT_PLATFORMS = ["instagram", "facebook", "xing", "linkedin"]


def _norm_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.strip().lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(src, dst)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


def _load_contacts_by_norm_name(conn, account_name: str | None) -> dict[str, list[dict]]:
    where_clause, params = _account_filter_clause(account_name)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT id, full_name, prefix, given_name, middle_name, family_name, suffix, photo_url
                FROM contacts {where_clause}""",
            params,
        )
        rows = cur.fetchall()
    mapping: dict[str, list[dict]] = {}
    for row in rows:
        if not row.get("full_name"):
            row["full_name"] = db._build_full_name(row)
        key = _norm_name(row.get("full_name"))
        if key:
            mapping.setdefault(key, []).append(row)
    return mapping


def _fetch_chat_top(platform: str | None, limit: int) -> list[dict]:
    resp = requests.get(
        f"{Config.CHATAPI_URL.rstrip('/')}/contacts/top",
        params={"platform": platform or None, "limit": limit},
        headers={"X-API-Key": Config.CHATAPI_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _fetch_conversation(names: list[str], offset: int, limit: int, order: str = "desc") -> dict:
    resp = requests.get(
        f"{Config.CHATAPI_URL.rstrip('/')}/conversation",
        params={
            "contact_names": names,
            "order": order,
            "offset": offset,
            "limit": limit,
        },
        headers={"X-API-Key": Config.CHATAPI_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


CHAT_EXPORT_MAX_MESSAGES = 50000


def _fetch_all_chat_messages(names: list[str]) -> tuple[list[dict], int, bool]:
    messages: list[dict] = []
    total = 0
    truncated = False
    offset = 0
    while True:
        data = _fetch_conversation(names, offset, 1000, order="asc")
        total = data.get("total") or 0
        batch = data.get("messages") or []
        if not batch:
            break
        messages.extend(batch)
        offset += len(batch)
        if offset >= total:
            break
        if offset >= CHAT_EXPORT_MAX_MESSAGES:
            truncated = True
            break
    if total > CHAT_EXPORT_MAX_MESSAGES:
        truncated = True
    return messages, total, truncated


def _slugify_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", _norm_name(name)).strip("-")
    return (s or "kontakt")[:80]


def _md_typed(item: dict, key: str = "type") -> str:
    t = (item.get(key) or "").strip()
    return f" ({t})" if t else ""


def _fmt_address_md(a: dict) -> str:
    parts = [
        a.get("street"),
        " ".join(p for p in [a.get("zip"), a.get("city")] if p and str(p).strip()),
        a.get("region"),
        a.get("country"),
    ]
    return ", ".join(p.strip() for p in parts if p and str(p).strip())


def _fmt_ms(ms: float | int, fmt: str) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(Config.TIMEZONE).strftime(fmt)


def _build_chat_markdown(
    title: str,
    contact: dict | None,
    messages: list[dict],
    total: int,
    truncated: bool,
    own_names: list[str],
) -> str:
    lines: list[str] = [
        f"# Chat-Archiv: {title}",
        "",
        f"Exportiert am {datetime.now(Config.TIMEZONE).strftime('%d.%m.%Y %H:%M')} aus iCloud Contacts Sync.",
        "",
    ]
    if truncated:
        lines += [f"**Hinweis:** Export auf die {len(messages)} ältesten Nachrichten begrenzt.", ""]

    if contact:
        lines += ["## Stammdaten", ""]
        simple_fields = [
            ("Name", contact.get("full_name")),
            ("Spitzname", contact.get("nickname")),
            ("Organisation", contact.get("organization")),
            ("Abteilung", contact.get("department")),
            ("Job-Titel", contact.get("job_title")),
            ("Geburtstag", contact["birthday"].strftime("%d.%m.%Y") if contact.get("birthday") else None),
            ("Jahrestag", contact["anniversary"].strftime("%d.%m.%Y") if contact.get("anniversary") else None),
            ("Konto", contact.get("account")),
        ]
        for label, value in simple_fields:
            if value:
                lines.append(f"- **{label}:** {value}")
        for label, key in (("E-Mail", "emails"), ("Telefon", "phones"), ("Website", "urls")):
            for item in contact.get(key) or []:
                if item.get("value"):
                    lines.append(f"- **{label}**{_md_typed(item)}: {item['value']}")
        for a in contact.get("addresses") or []:
            addr = _fmt_address_md(a)
            if addr:
                lines.append(f"- **Adresse**{_md_typed(a)}: {addr}")
        for sp in contact.get("social_profiles") or []:
            detail = sp.get("url") or sp.get("username")
            if detail:
                lines.append(f"- **Soziales Profil** ({sp.get('type') or 'other'}): {detail}")
        for r in contact.get("related_names") or []:
            if r.get("value"):
                lines.append(f"- **Beziehung** ({r.get('type') or 'Sonstige'}): {r['value']}")
        if contact.get("categories"):
            lines.append(f"- **Kategorien:** {', '.join(contact['categories'])}")
        if contact.get("groups"):
            lines.append(f"- **Gruppen:** {', '.join(contact['groups'])}")
        if contact.get("updated_at"):
            lines.append(f"- **Kontakt zuletzt synchronisiert:** {contact['updated_at']}")

        notes = (contact.get("notes") or "").strip()
        lines += ["", "## Notizen", ""]
        lines.append(notes if notes else "Keine Notizen hinterlegt.")

    lines += ["", "## Chat-Übersicht", ""]
    own_norms = {_norm_name(n) for n in own_names if n}
    if messages:
        own_count = sum(1 for m in messages if _norm_name(m.get("sender_name")) in own_norms)
        first_ts = messages[0].get("timestamp_ms") or 0
        last_ts = messages[-1].get("timestamp_ms") or 0
        platforms = sorted({m["platform"] for m in messages if m.get("platform")})
        lines.append(f"- **Nachrichten gesamt:** {total}")
        if first_ts:
            lines.append(
                f"- **Zeitraum:** {_fmt_ms(first_ts, '%d.%m.%Y %H:%M')} – {_fmt_ms(last_ts, '%d.%m.%Y %H:%M')}"
            )
            lines.append(f"- **Letzter Kontakt:** {_fmt_ms(last_ts, '%d.%m.%Y %H:%M')}")
        if platforms:
            lines.append(f"- **Plattformen:** {', '.join(platforms)}")
        lines.append(f"- **Von dir:** {own_count} · **Von {title}:** {len(messages) - own_count}")
    else:
        lines.append("Keine Nachrichten vorhanden.")

    if messages:
        lines += ["", "## Chat-Verlauf", "", "Chronologisch, älteste Nachricht zuerst.", ""]
    else:
        lines += ["", "## Chat-Verlauf", "", "Keine Nachrichten vorhanden.", ""]
    current_day = None
    for m in messages:
        ts = m.get("timestamp_ms") or 0
        day = _fmt_ms(ts, "%Y-%m-%d") if ts else "Unbekanntes Datum"
        time_str = _fmt_ms(ts, "%H:%M") if ts else "--:--"
        if day != current_day:
            current_day = day
            lines += [f"### {day}", ""]
        sender = m.get("sender_name") or "Unbekannt"
        is_own = _norm_name(sender) in own_norms
        label = "Ich" if is_own else sender
        platform = m.get("platform")
        suffix = f" ({platform})" if platform and not is_own else ""
        lines.append(f"**[{time_str}] {label}**{suffix}:")
        lines.append("")
        content = (m.get("content") or "").strip()
        mtype = m.get("message_type") or ""
        lines.append(content if content else f"*[{mtype}]*" if mtype else "*(leere Nachricht)*")
        for r in m.get("reactions") or []:
            emoji = r.get("reaction") or r.get("emoji") or ""
            user = r.get("user")
            lines.append(f"→ Reaktion: {emoji}" + (f" von {user}" if user else ""))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _markdown_response(title: str, markdown: str) -> Response:
    filename = f"chat-{_slugify_name(title)}-{datetime.now(Config.TIMEZONE).strftime('%Y-%m-%d')}.md"
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _load_contact_export_row(conn, account_name: str | None, contact_id: int) -> dict | None:
    where_clause, params = _account_filter_clause(account_name)
    id_clause = "AND id = %s" if where_clause else "WHERE id = %s"
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT id, account, uid, full_name, prefix, given_name, middle_name, family_name, suffix,
                       nickname, organization, job_title, department, birthday, anniversary, notes,
                       emails, phones, addresses, urls, social_profiles, related_names, categories, updated_at
                FROM contacts {where_clause} {id_clause}""",
            params + [contact_id],
        )
        return cur.fetchone()


def _contact_chat_sender_name(contact: dict | None) -> str:
    if not contact:
        return ""
    for acc in Config.load_accounts():
        if acc.name == contact.get("account"):
            return acc.chat_sender_name
    return ""


def _match_chat_top(conn, account_name: str | None, platform: str | None, limit: int) -> list[dict]:
    mapping = _load_contacts_by_norm_name(conn, account_name)
    results = []
    for item in _fetch_chat_top(platform, limit):
        name = item.get("name") or ""
        matches = mapping.get(_norm_name(name), [])
        entry = {
            "name": name,
            "message_count": item.get("message_count", 0),
            "contact_id": None,
            "full_name": None,
            "photo_url": None,
            "matched": False,
            "chat_url": None,
            "search_url": f"/search?search={quote_plus(name)}" if name else None,
        }
        if not matches and name:
            entry["chat_url"] = f"/chat/person?name={quote_plus(name)}"
        elif len(matches) == 1:
            contact = matches[0]
            entry["contact_id"] = contact["id"]
            entry["full_name"] = contact["full_name"]
            entry["photo_url"] = contact.get("photo_url")
            entry["matched"] = True
            entry["chat_url"] = None
            entry["search_url"] = None
        results.append(entry)
    return results


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
                    ORDER BY given_name, family_name, full_name
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
                f"""SELECT id, account, uid, full_name, prefix, given_name, middle_name, family_name, suffix, organization,
                           job_title, birthday, notes, photo_url, emails, phones, addresses, urls, social_profiles, related_names, categories, updated_at
                    FROM contacts {where_clause} {id_clause}""",
                params + [contact_id],
            )
            row = cur.fetchone()

        if not row:
            return {}

        groups = db.get_groups_for_contact(conn, row["account"], row["uid"])
        group_names = [g["name"] for g in groups if g.get("name")]

        contact = _row_to_contact_out(row, group_names=group_names)
        _enrich_related_names(conn, contact)
    return contact


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
                    ORDER BY given_name, family_name, full_name""",
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


@app.get("/api/contacts/{contact_id}/messages")
def get_contact_messages(
    contact_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: str = Depends(get_current_user),
):
    if not Config.CHATAPI_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "Chat-Archive nicht aktiviert"})

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(
            resolve_account_for_user(current_user)[0]
        )
        id_clause = "AND id = %s" if where_clause else "WHERE id = %s"
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT full_name, prefix, given_name, middle_name, family_name, suffix
                    FROM contacts {where_clause} {id_clause}""",
                params + [contact_id],
            )
            row = cur.fetchone()

    if not row:
        return JSONResponse(status_code=404, content={"detail": "Kontakt nicht gefunden"})

    if not row.get("full_name"):
        row["full_name"] = db._build_full_name(row)

    if not row.get("full_name"):
        return JSONResponse(status_code=404, content={"detail": "Kontakt hat keinen Namen"})

    try:
        return _fetch_conversation([row["full_name"]], offset, limit)
    except requests.RequestException as e:
        logger.warning("Chat-Archive API Fehler: %s", e)
        return JSONResponse(status_code=502, content={"detail": "Chat-Archive nicht erreichbar"})


@app.get("/api/chat/messages")
def api_chat_messages(
    name: str = Query(...),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: str = Depends(get_current_user),
):
    if not Config.CHATAPI_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "Chat-Archive nicht aktiviert"})

    name = name.strip()
    if not name:
        return JSONResponse(status_code=400, content={"detail": "Name fehlt"})

    try:
        return _fetch_conversation([name], offset, limit)
    except requests.RequestException as e:
        logger.warning("Chat-Archive API Fehler: %s", e)
        return JSONResponse(status_code=502, content={"detail": "Chat-Archive nicht erreichbar"})


@app.get("/api/contacts/{contact_id}/messages/export")
def export_contact_messages(contact_id: int, current_user: str = Depends(get_current_user)):
    if not Config.CHATAPI_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "Chat-Archive nicht aktiviert"})

    account_name = resolve_account_for_user(current_user)[0]
    with db.get_connection() as conn:
        row = _load_contact_export_row(conn, account_name, contact_id)
        group_names = []
        if row:
            group_names = [
                g["name"] for g in db.get_groups_for_contact(conn, row["account"], row["uid"]) if g.get("name")
            ]

    if not row:
        return JSONResponse(status_code=404, content={"detail": "Kontakt nicht gefunden"})

    contact = _row_to_contact_out(row, group_names=group_names)
    full_name = contact.get("full_name")
    if not full_name:
        return JSONResponse(status_code=404, content={"detail": "Kontakt hat keinen Namen"})

    chat_sender = _contact_chat_sender_name(contact)
    try:
        messages, total, truncated = _fetch_all_chat_messages([full_name])
    except requests.RequestException as e:
        logger.warning("Chat-Archive API Fehler: %s", e)
        return JSONResponse(status_code=502, content={"detail": "Chat-Archive nicht erreichbar"})

    markdown = _build_chat_markdown(
        title=full_name,
        contact=contact,
        messages=messages,
        total=total,
        truncated=truncated,
        own_names=[chat_sender] if chat_sender else [],
    )
    return _markdown_response(full_name, markdown)


@app.get("/api/chat/export")
def export_chat_by_name(name: str = Query(...), current_user: str = Depends(get_current_user)):
    if not Config.CHATAPI_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "Chat-Archive nicht aktiviert"})

    name = name.strip()
    if not name:
        return JSONResponse(status_code=400, content={"detail": "Name fehlt"})

    account_name = resolve_account_for_user(current_user)[0]
    contact = None
    with db.get_connection() as conn:
        matches = _load_contacts_by_norm_name(conn, account_name).get(_norm_name(name), [])
        if len(matches) == 1:
            row = _load_contact_export_row(conn, account_name, matches[0]["id"])
            if row:
                group_names = [
                    g["name"]
                    for g in db.get_groups_for_contact(conn, row["account"], row["uid"])
                    if g.get("name")
                ]
                contact = _row_to_contact_out(row, group_names=group_names)

    own_names = [n for n in [_contact_chat_sender_name(contact)] if n]
    if not own_names:
        own_names = [
            a.chat_sender_name
            for a in Config.load_accounts()
            if a.chat_sender_name and (account_name is None or a.name == account_name)
        ]

    title = (contact.get("full_name") if contact else None) or name
    try:
        messages, total, truncated = _fetch_all_chat_messages([name])
    except requests.RequestException as e:
        logger.warning("Chat-Archive API Fehler: %s", e)
        return JSONResponse(status_code=502, content={"detail": "Chat-Archive nicht erreichbar"})

    markdown = _build_chat_markdown(
        title=title,
        contact=contact,
        messages=messages,
        total=total,
        truncated=truncated,
        own_names=own_names,
    )
    return _markdown_response(title, markdown)


@app.get("/api/chat/top", response_model=ChatTopResponse)
def api_chat_top(
    platform: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    current_user: str = Depends(get_current_user),
):
    if not Config.CHATAPI_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "Chat-Archive nicht aktiviert"})

    platform = platform or None
    if platform and platform not in CHAT_PLATFORMS:
        return JSONResponse(status_code=400, content={"detail": "Unbekannte Plattform"})

    account_name = resolve_account_for_user(current_user)[0]
    try:
        with db.get_connection() as conn:
            items = _match_chat_top(conn, account_name, platform, limit)
    except requests.RequestException as e:
        logger.warning("Chat-Archive API Fehler: %s", e)
        return JSONResponse(status_code=502, content={"detail": "Chat-Archive nicht erreichbar"})

    return {"platform": platform, "total": len(items), "items": items}


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
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: str = Depends(get_current_user),
):
    account_name, _ = resolve_account_for_user(current_user)

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
    account_name, _ = resolve_account_for_user(current_user)

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
                   JOIN `groups` g ON g.id = gm.group_id
                   LEFT JOIN contacts c ON c.account = g.account AND c.uid = gm.member_uid
                   WHERE g.id = %s
                   ORDER BY c.given_name, c.family_name, c.full_name""",
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
    account_name, _ = resolve_account_for_user(current_user)

    with db.get_connection() as conn:
        where_clause, params = _account_filter_clause(account_name)
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT g.id, g.account FROM `groups` g {where_clause}
                    {"AND" if where_clause else "WHERE"} g.id = %s""",
                params + [group_id],
            )
            group = cur.fetchone()
            if not group:
                return {}

            cur.execute(
                """SELECT gm.member_uid, c.id, c.full_name, c.given_name, c.family_name,
                          c.organization, c.birthday, c.photo_url
                   FROM group_members gm
                   JOIN `groups` g ON g.id = gm.group_id
                   LEFT JOIN contacts c ON c.account = g.account AND c.uid = gm.member_uid
                   WHERE gm.group_id = %s
                   ORDER BY c.given_name, c.family_name, c.full_name""",
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
        groups = db.get_all_groups(conn, account_name)
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
        request,
        "dashboard.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
            "show_all": show_all,
            "account_name": account_name or "alle Accounts",
            "contact_count": contact_count,
            "upcoming_birthdays": upcoming_birthdays,
            "last_sync": last_sync,
            "last_sync_with_changes": last_sync_with_changes,
            "groups": groups,
            "chat_enabled": Config.CHATAPI_ENABLED,
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
        request,
        "admin.html",
        {
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

    try:
        Config.validate_mailer()
    except RuntimeError as exc:
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "current_user": current_user,
                "show_all": request.session.get("show_all", False),
                "error": str(exc),
            },
            status_code=400,
        )

    accounts = Config.load_accounts()
    mail_accounts = [a for a in accounts if a.birthday_mail_to]
    if not mail_accounts:
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "current_user": current_user,
                "show_all": request.session.get("show_all", False),
                "error": "Keine Accounts mit birthday_mail_to konfiguriert",
            },
            status_code=400,
        )

    with db.get_connection() as conn:
        target = db.get_most_common_birthday(conn)
        if target is None:
            return templates.TemplateResponse(
                request,
                "admin.html",
                {
                    "current_user": current_user,
                    "show_all": request.session.get("show_all", False),
                    "error": "Keine Kontakte mit Geburtstag in der Datenbank",
                },
                status_code=400,
            )

        sent_count = 0
        errors = []
        for account in mail_accounts:
            birthdays = fetch_todays_birthdays_for_account(conn, account.name, target)
            if not birthdays:
                continue
            msg = build_message(account.name, birthdays, target_date=target)
            msg["To"] = account.birthday_mail_to
            try:
                send_message(msg)
                sent_count += 1
            except Exception as exc:
                errors.append(f"{account.name}: {exc}")

    if errors:
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "current_user": current_user,
                "show_all": request.session.get("show_all", False),
                "error": f"Versand fehlgeschlagen: {'; '.join(errors)}",
            },
            status_code=500,
        )

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "current_user": current_user,
            "show_all": request.session.get("show_all", False),
            "success": f"Test-Mails für {target.strftime('%d.%m.%Y')} gesendet ({sent_count} Accounts).",
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
        "last_updated": db.search_contacts_last_updated,
    }.get(type)

    if not query_fn:
        return RedirectResponse(url="/", status_code=303)

    title_map = {
        "no_photo": "Kontakte ohne Bild",
        "no_city": "Kontakte ohne Stadt",
        "no_social": "Kontakte ohne Social Profil",
        "last_updated": "Zuletzt aktualisiert",
    }

    with db.get_connection() as conn:
        rows = query_fn(conn, account_name)
        groups = db.get_all_groups(conn, account_name)

    for row in rows:
        if not row.get("full_name"):
            row["full_name"] = db._build_full_name(row)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
            "show_all": show_all,
            "account_name": account_name or "alle Accounts",
            "contacts": rows,
            "search": "",
            "groups": groups,
            "chat_enabled": Config.CHATAPI_ENABLED,
            "search_title": title_map.get(type, "Suche"),
        },
    )


@app.get("/search", response_class=HTMLResponse)
def web_search(
    request: Request,
    search: str | None = Query(default=None),
    group: str | None = Query(default=None),
    current_user: str = Depends(get_current_user),
):
    account_name, is_admin, show_all = _resolve_effective_account(request, current_user)

    with db.get_connection() as conn:
        groups = db.get_all_groups(conn, account_name)

        if group:
            rows = db.get_contacts_by_group_uid(conn, account_name, group)
            search_title = None
            for g in groups:
                if g["uid"] == group:
                    search_title = g["name"]
                    break
        else:
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
                        ORDER BY given_name, family_name, full_name
                        LIMIT 200""",
                    params,
                )
                rows = cur.fetchall()
            search_title = None

    for row in rows:
        if not row.get("full_name"):
            row["full_name"] = db._build_full_name(row)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
            "show_all": show_all,
            "account_name": account_name or "alle Accounts",
            "contacts": rows,
            "search": search or "",
            "groups": groups,
            "chat_enabled": Config.CHATAPI_ENABLED,
            "search_title": search_title,
        },
    )


@app.get("/chat/top", response_class=HTMLResponse)
def web_chat_top(
    request: Request,
    platform: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    current_user: str = Depends(get_current_user),
):
    if not Config.CHATAPI_ENABLED:
        return RedirectResponse(url="/", status_code=303)

    account_name, is_admin, show_all = _resolve_effective_account(request, current_user)

    platform = platform or None
    if platform and platform not in CHAT_PLATFORMS:
        platform = None

    results = []
    error = None
    try:
        with db.get_connection() as conn:
            results = _match_chat_top(conn, account_name, platform, limit)
            groups = db.get_all_groups(conn, account_name)
    except requests.RequestException as e:
        logger.warning("Chat-Archive API Fehler: %s", e)
        error = "Chat-Archive nicht erreichbar"
        groups = []

    return templates.TemplateResponse(
        request,
        "chat_top.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
            "show_all": show_all,
            "account_name": account_name or "alle Accounts",
            "groups": groups,
            "chat_enabled": Config.CHATAPI_ENABLED,
            "platforms": CHAT_PLATFORMS,
            "selected_platform": platform or "",
            "results": results,
            "error": error,
        },
    )


@app.get("/chat/person", response_class=HTMLResponse)
def web_chat_person(
    request: Request,
    name: str = Query(...),
    current_user: str = Depends(get_current_user),
):
    if not Config.CHATAPI_ENABLED:
        return RedirectResponse(url="/", status_code=303)

    name = name.strip()
    if not name:
        return RedirectResponse(url="/chat/top", status_code=303)

    account_name, is_admin, show_all = _resolve_effective_account(request, current_user)

    with db.get_connection() as conn:
        groups = db.get_all_groups(conn, account_name)

    accounts = Config.load_accounts()
    own_names = [
        a.chat_sender_name for a in accounts
        if a.chat_sender_name and (account_name is None or a.name == account_name)
    ]

    return templates.TemplateResponse(
        request,
        "chat_person.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
            "show_all": show_all,
            "account_name": account_name or "alle Accounts",
            "groups": groups,
            "chat_enabled": Config.CHATAPI_ENABLED,
            "chat_name": name,
            "chat_messages_url": "/api/chat/messages?name=" + quote_plus(name),
            "chat_export_url": "/api/chat/export?name=" + quote_plus(name),
            "chat_own_names": own_names,
            "search_url": "/search?search=" + quote_plus(name),
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
                f"""SELECT id, account, uid, full_name, prefix, given_name, middle_name, family_name, suffix, organization,
                           job_title, birthday, notes, photo_url, emails, phones, addresses, urls, social_profiles, related_names, categories, updated_at
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
        _enrich_related_names(conn, contact)

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
    chat_sender_name = ""
    contact_account = contact.get("account")
    if contact_account:
        accounts = Config.load_accounts()
        for acc in accounts:
            if acc.name == contact_account:
                custom_links = acc.custom_links
                chat_sender_name = acc.chat_sender_name
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
        request,
        "contact.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
            "show_all": show_all,
            "contact": contact,
            "groups": groups,
            "search": search or "",
            "custom_links": resolved_links,
            "chat_enabled": Config.CHATAPI_ENABLED,
            "chat_own_names": [chat_sender_name] if chat_sender_name else [],
            "chat_messages_url": f"/api/contacts/{contact_id}/messages",
            "chat_export_url": f"/api/contacts/{contact_id}/messages/export",
        },
    )
