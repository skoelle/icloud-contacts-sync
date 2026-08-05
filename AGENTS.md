# AGENTS.md — iCloud Contacts Sync

## Project Overview

Automated CardDAV delta-sync (RFC 6578) of multiple iCloud accounts into a shared MariaDB database, with a daily birthday email mailer and a read-only web UI/API behind Authelia. Runs as Docker containers on a Proxmox host.

## Tech Stack

- **Language:** Python 3.12
- **Framework:** FastAPI (API), PyMySQL (DB), vobject (vCard), lxml (XML), requests (HTTP)
- **Database:** MariaDB (InnoDB, utf8mb4)
- **Container:** python:3.12-slim, supercronic for cron
- **CI/CD:** GitHub Actions — lint (ruff), build+push (ghcr.io)
- **Auth:** Authelia reverse-proxy header (`Remote-User`), no built-in login

## Repository Structure

```
src/
  sync.py            — Main sync orchestrator (cron entry point)
  mailer.py          — Birthday email sender (cron entry point)
  config.py          — Reads accounts.yml + env vars
  db.py              — MariaDB connection and queries
  carddav_client.py  — CardDAV HTTP client (sync-collection, addressbook-query)
  vcard_parser.py    — vCard parsing via vobject
  scheduler.py       — supercronic crontab generation
  api/
    main.py          — FastAPI app (uvicorn entry point)
    auth.py          — Remote-User header dependency
    schemas.py       — Pydantic response models
    static/          — CSS/JS assets
    templates/       — Jinja2 HTML templates
sql/
  schema.sql         — Full schema (contacts, sync_state, sync_runs, birthday_mail_log)
  db-and-user.sql    — One-time DB + user setup
config/
  accounts.yml       — Per-account credentials (NOT in git, volume-mounted)
docker/
  entrypoint.sh      — Generates crontab, starts supercronic
```

## Key Files

| File | Role |
|------|------|
| `src/sync.py` | CLI entry point for sync (`python3 sync.py`) |
| `src/mailer.py` | CLI entry point for birthday mailer (`python3 mailer.py`) |
| `src/api/main.py` | FastAPI app entry point (`uvicorn api.main:app`) |
| `src/config.py` | Loads `accounts.yml` + all env vars |
| `src/db.py` | All MariaDB queries |
| `sql/schema.sql` | Canonical schema definition |
| `.env.example` | All supported environment variables |

## Commands

### Lint

```bash
ruff check src/
```

### Run sync locally (no Docker)

```bash
cd src && python3 sync.py
```

### Run mailer locally

```bash
cd src && python3 mailer.py
```

### Docker build

```bash
docker compose build
```

### Docker run

```bash
docker compose up -d
```

## Code Conventions

- All source in `src/`, single package, no `setup.py`/`pyproject.toml`.
- No comments in code unless explicitly requested.
- Follow existing code style; no new dependencies unless absolutely necessary.
- Secrets must never be committed. `config/accounts.yml` and `.env` are gitignored.
- The DB schema uses `account` column as tenant key — all queries are scoped per account.
- JSON columns (`emails`, `phones`, etc.) store multi-value vCard fields.

## Database Schema

Four tables:
- `contacts` — All synced contacts (unique on `(account, uid)`)
- `sync_state` — Per-account CardDAV sync token for delta sync
- `sync_runs` — Sync run history with status and stats
- `birthday_mail_log` — Prevents duplicate birthday emails on same day

Schema is defined in `sql/schema.sql`. Always update schema.sql when changing the data model.

## Environment Variables

See `.env.example` for full list. Key variables:
- `MARIADB_*` — Database connection
- `SMTP_*` / `MAIL_*` — Birthday mailer
- `AUTH_REMOTE_USER_HEADER` — Authelia header name (default: `Remote-User`)
- `MAILER_ENABLED` — Feature flag for birthday mailer
- `MAIL_SEND_HOUR` — Hour (0-23) for daily birthday email

## Architecture Notes

- Single Docker image, two roles: cron (sync+mailer) and API (uvicorn).
- API is read-only; only the sync container writes to MariaDB.
- Sync uses CardDAV `sync-collection` (RFC 6578) for efficient delta sync.
- Token expiry (~29 days) triggers automatic full re-sync.
- Deleted contacts are removed from DB (no archival).

## Common Tasks

### Adding a new API endpoint
1. Add route in `src/api/main.py`
2. Add Pydantic model in `src/api/schemas.py` if needed
3. Add DB query in `src/db.py` if needed
4. Test with: `curl -H "Remote-User: <user>" http://127.0.0.1:8000/<path>`

### Adding a new contact field
1. Add column to `contacts` table in `sql/schema.sql`
2. Update `src/vcard_parser.py` to extract the field
3. Update `src/db.py` upsert query
4. Update `src/api/schemas.py` if exposing via API

### Changing the sync logic
1. Edit `src/carddav_client.py` for CardDAV protocol changes
2. Edit `src/sync.py` for orchestration changes
3. Test with a single account first: set `LOG_LEVEL=DEBUG`
