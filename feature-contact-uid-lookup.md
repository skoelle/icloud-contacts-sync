# Feature: Contact Detail Page — UID statt DB-ID

## Status

**Geplant (Backlog)** — Kein aktueller Bedarf, aber sinnvolles Zukunftsfuture.

## Ausgangslage

Die Contact-Detail-Seite wird aktuell über die interne Datenbank-ID (`id INT AUTO_INCREMENT`) aufgerufen:

```
/contacts/{contact_id}        (HTML)
/api/contacts/{contact_id}    (JSON)
```

Die `id` kann sich bei einem Re-Sync (z.B. nach Datenverlust) ändern. Die **UID** aus CardDAV ist hingegen stabil und bleibt über Synchronisationen hinweg gleich.

## Problem

- Die `id` ist kein stabiler Identifier — sie kann sich ändern
- Bei einem Re-Sync mit neuem Schema werden alle alten IDs ungültig
- Geteilte Links oder Lesezeichen auf `/contacts/42` brechen

## Lösungsidee

Den Lookup von `WHERE id = %s` auf `WHERE uid = %s` umstellen.

### Einschränkung

UIDs sind pro Account eindeutig (`UNIQUE KEY (account, uid)`), nicht global. Für Admin-Nutzer mit `show_all=True` müsste der Account ebenfalls im Lookup berücksichtigt werden.

### Mögliche URL-Patterns

| Ansatz | URL | Vorteil | Nachteil |
|--------|-----|---------|----------|
| UID only | `/contacts/{uid}` | Einfach | Nur sicher, wenn UID global eindeutig |
| Account + UID | `/contacts/{account}/{uid}` | Explizit, immer korrekt | Längere URL, Admin-Modus nötig |
| Komposit | `/contacts/{account}--{uid}` | Ein Path-Parameter | Unschön |

**Empfehlung:** Account + UID (`/contacts/{account}/{uid}`) — sauber und eindeutig.

## Betroffene Dateien

### 1. `src/api/main.py` — API-Routen

**JSON API (Zeile 127-149):**
- Route: `GET /api/contacts/{contact_id}` → `GET /api/contacts/{account}/{contact_uid}`
- Parameter: `contact_id: int` → `account: str, contact_uid: str`
- Query: `WHERE id = %s` → `WHERE uid = %s AND account = %s`

**HTML Detail (Zeile 578-601):**
- Route: `GET /contacts/{contact_id}` → `GET /contacts/{account}/{contact_uid}`
- Parameter: `contact_id: int` → `account: str, contact_uid: str`
- Query: `WHERE id = %s` → `WHERE uid = %s AND account = %s`

### 2. `src/api/templates/index.html` — Kontaktliste

**Zeile 193:**
```html
<!-- Aktuell -->
<a href="/contacts/{{ c.id }}{% if search %}?search={{ search }}{% endif %}">

<!-- Neu -->
<a href="/contacts/{{ c.account }}/{{ c.uid | urlencode }}{% if search %}?search={{ search }}{% endif %}">
```

### 3. `src/api/templates/dashboard.html` — Geburtstagsliste

**Zeile 309:**
```html
<!-- Aktuell -->
<a href="/contacts/{{ b.id }}" ...>

<!-- Neu -->
<a href="/contacts/{{ b.account }}/{{ b.uid | urlencode }}" ...>
```

### 4. `src/mailer.py` — Birthday-Mails

**Zeile 25:** SELECT um `uid` erweitern (oder `id` entfernen):
```sql
SELECT account, uid, full_name, ...
```

**Zeile 84 (Plain Text):**
```python
# Aktuell
line += f"  → {Config.WEB_URL.rstrip('/')}/contacts/{b['id']}"
# Neu
line += f"  → {Config.WEB_URL.rstrip('/')}/contacts/{b['account']}/{b['uid']}"
```

**Zeile 95 (HTML):**
```python
# Aktuell
contact_link = f"{Config.WEB_URL.rstrip('/')}/contacts/{b['id']}"
# Neu
contact_link = f"{Config.WEB_URL.rstrip('/')}/contacts/{b['account']}/{b['uid']}"
```

### 5. `src/api/schemas.py` — Pydantic Models

- `ContactOut.id` (Zeile 9): Kann bleiben (nützlich für interne Zwecke), aber `uid` wird primärer Lookup-Key
- `GroupMemberOut.id` (Zeile 53): Prüfen ob noch benötigt

### 6. `src/db.py` — DB-Queries

SELECT-Klauseln enthalten bereits `uid`. `id` kann aus SELECTs entfernt werden, wo es nicht gebraucht wird.

### 7. Dokumentation

- `README.md` (Zeilen 166, 244, 247): Endpoint-Pattern aktualisieren
- `SPEC.md` (Zeilen 253, 256): Endpoint-Pattern aktualisieren

## Risiken

1. **Backward-Kompatibilität:** Bestehende URLs (`/contacts/42`) brechen. Kein eleganter Redirect möglich (alte ID sagt nichts über UID aus).
2. **UID-Encoding:** UIDs aus CardDAV können Sonderzeichen enthalten — `urlencode` in Templates ist Pflicht.
3. **Admin-Modus:** Bei `show_all=True` ist `account_name` aktuell `None`. Der neue Ansatz mit explizitem Account-Pfad löst das sauber auf.

## Umsetzungsreihenfolge

1. `src/api/main.py` — Routen ändern (API + HTML)
2. `src/api/templates/index.html` — Links anpassen
3. `src/api/templates/dashboard.html` — Links anpassen
4. `src/mailer.py` — URL-Bau anpassen
5. `src/api/schemas.py` — `GroupMemberOut.id` prüfen
6. `README.md` + `SPEC.md` — Dokumentation aktualisieren
7. `ruff check src/` — Lint-Check
