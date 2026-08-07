# SPEC: iCloud Contacts Sync (v2)

## 1. Zweck

Automatisierter, wiederkehrender Delta-Sync mehrerer iCloud-Accounts
(CardDAV) in eine gemeinsame MariaDB-Instanz (`mariadb.internal`),
inklusive täglichem Mailversand für heutige Geburtstage. Ziel ist eine
vollständige, queryfähige Kopie aller Kontaktdaten mehrerer Apple-IDs
außerhalb des Apple-Ökosystems.

## 2. Architektur

```
+-------------------+   CardDAV (HTTPS, Basic Auth, je Account)   +----------------------+
| icloud-contacts-  | -------------------------------------------> | contacts.icloud.com |
| sync Container    |                                              +----------------------+
| (Docker-Host auf    |
|  Proxmox-Host)       |   MySQL Protocol (TCP 3306)
|                   | -------------------------------------------> mariadb.internal
|                   |
|                   |   SMTP (Port 587, STARTTLS)
|                   | -------------------------------------------> SMTP-Relay
+-------------------+
```

- Ein Container verarbeitet sequenziell alle in `config/accounts.yml`
  konfigurierten Apple-IDs, jeweils isoliert mit eigenem sync-token und
  eigenem `account`-Feld in der Datenbank.
- Zwei unabhängige Cron-Jobs innerhalb desselben Containers:
  Kontakt-Sync (alle 15 Minuten) und Geburtstags-Mailer (täglich,
  konfigurierbare Uhrzeit).
- Zeitsteuerung über `supercronic`, Crontab wird beim Container-Start
  dynamisch aus `MAIL_SEND_HOUR` generiert.

## 3. Multi-User-Konfiguration

- Datei `config/accounts.json` (gemountet, nicht im Image, nicht im Git,
  siehe `.gitignore`), Struktur:
  ```json
  {
    "accounts": [
      { "name": "markus", "apple_email": "markus@icloud.com", "apple_app_password": "xxxx-xxxx-xxxx-xxxx", "authelia_user": "mmustermann", "birthday_mail_to": "markus@example.de", "healthcheck_url": "https://healthchecks.example.de/ping/abc123" },
      { "name": "partner", "apple_email": "partner@icloud.com", "apple_app_password": "yyyy-yyyy-yyyy-yyyy", "authelia_user": "pmustermann", "birthday_mail_to": "partner@example.de" }
    ],
    "admins": ["mmustermann"]
  }
  ```
- `name` ist der interne, eindeutige Account-Bezeichner und wird 1:1 als
  `account`-Spalte in `contacts`, `sync_state` und `sync_runs`
  gespeichert.
- Jeder Account wird beim Sync-Lauf unabhängig verarbeitet: ein
  Fehler bei einem Account (z. B. abgelaufenes App-Passwort) bricht den
  Lauf für andere Accounts nicht ab.
- Die Datei liegt bewusst separat von `.env`, da sie mehrere
  Credential-Sets enthält und sich unabhängig von der übrigen
  Konfiguration versionieren/rotieren lässt.

## 4. Delta-Sync über CardDAV sync-collection (RFC 6578)

- Für jeden Account wird nach der Collection-Discovery ein
  `REPORT sync-collection` mit dem zuletzt gespeicherten `sync-token`
  ausgeführt; der Server liefert nur geänderte, neue und gelöschte
  Kontakte seit diesem Token zurück.
- Der neue `sync-token` wird nach jedem erfolgreichen Lauf pro Account
  in `sync_state` gespeichert.
- **Initialer Lauf**: Existiert noch kein Token, wird einmalig ein
  vollständiger Abruf per `addressbook-query` durchgeführt
  (`sync_type = 'initial'` in `sync_runs`), anschließend wird der erste
  `sync-token` gespeichert.
- **Token-Ablauf**: iCloud-Tokens sind laut Beobachtung ca. 29 Tage
  gültig. Lehnt der Server einen Token ab (`403 valid-sync-token`),
  löscht der Client den gespeicherten Token und führt automatisch einen
  vollen Re-Sync durch, ohne manuellen Eingriff.
- Gelöschte Kontakte werden über `404`-Status-Einträge in der
  sync-collection-Antwort erkannt (Href-basiert) und gezielt aus
  MariaDB entfernt, es findet kein pauschales Löschen aller Kontakte
  mehr statt (Unterschied zu v1).
- Vorteil bei 2.000+ Kontakten: reguläre 15-Minuten-Läufe übertragen nur
  die tatsächlichen Änderungen, nicht den kompletten Bestand.
- **Healthcheck-URL**: Das optionale Feld `healthcheck_url` pro Account
  in `accounts.json` wird nach jedem erfolgreichen Sync-Lauf dieses
  Accounts per `GET` aufgerufen (Timeout 10 Sekunden). Dient dem
  Uptime-Monitoring (z.B. Healthchecks.io, Uptime Kuma). Bei
  Sync-Fehlern oder wenn das Feld leer/fehlend ist, wird kein Aufruf
  ausgeführt. Fehler beim Aufruf werden geloggt, brechen den
  Sync-Prozess aber nicht ab.

## 5. Datenmodell (MariaDB)

Siehe `sql/schema.sql`. Wichtigste Änderungen gegenüber v1:

- `contacts.account` zusätzliche Spalte, Eindeutigkeit jetzt über
  `(account, uid)` statt `(uid, source)`, damit identische UIDs in
  unterschiedlichen Apple-IDs nicht kollidieren.
- Neue Tabelle `sync_state`: ein Datensatz pro Account mit dem
  aktuellen `sync_token`.
- `sync_runs` erweitert um `account`, `sync_type`
  (`initial`/`delta`), `contacts_upserted`, `contacts_deleted`.
- Neue Tabelle `birthday_mail_log`: ein Datensatz pro Tag und Account,
  an dem erfolgreich eine Geburtstagsmail versendet wurde, verhindert
  Doppelversand bei mehrfachem Container-Neustart am selben Tag.
- Neue Tabellen `groups` und `group_members`: Speichert
  iCloud-Kontaktgruppen (vCards mit `X-ADDRESSBOOKSERVER-KIND:group`)
  und deren Mitgliedschaften. Gruppen werden beim Sync erkannt und
  nicht als Kontakte in die `contacts`-Tabelle geschrieben.
  `group_members` referenziert `groups(id)` mit `ON DELETE CASCADE`.

## 6. Geburtstags-Mailer

- Eigenständiges Skript `src/mailer.py`, läuft im selben Container über
  einen zweiten Cron-Eintrag, täglich zur in `MAIL_SEND_HOUR`
  konfigurierten Stunde (Default 7 Uhr).
- Pro Account mit gesetztem `birthday_mail_to` wird eine eigene E-Mail
  versendet, die nur Geburtstage aus diesem Account enthält.
- Query: Kontakte des jeweiligen Accounts, deren `birthday`
  (Monat/Tag) auf das heutige Datum fällt.
- Versand nur bei existierenden Geburtstagen: Wird keine E-Mail
  versendet, wenn die Abfrage keine Treffer liefert.
- Versand per SMTP mit STARTTLS (`smtplib`), Konfiguration über
  `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM`.
  Die Empfänger-Adresse (`birthday_mail_to`) wird pro Account in
  `accounts.json` konfiguriert.
- Idempotenz: vor dem Versand wird `birthday_mail_log` auf einen
  Eintrag für den heutigen Tag und den jeweiligen Account geprüft;
  existiert bereits einer, wird der Lauf ohne erneuten Versand beendet.
- Feature-Flag `MAILER_ENABLED` erlaubt das komplette Deaktivieren ohne
  Codeänderung (Default: `false`).
- E-Mail-Inhalt: HTML-E-Mail mit stylisierten Geburtstagskarten
  (Name, Alter, Link zur Kontakt-Detailseite falls `WEB_URL`
  gesetzt). Zusätzlich reiner Text-Alternative als Fallback.

## 7. Konfiguration (Umgebungsvariablen)

| Variable              | Pflicht | Beschreibung                                     |
|-----------------------|---------|---------------------------------------------------|
| MARIADB_HOST          | nein    | Default: mariadb.internal                          |
| MARIADB_PORT          | nein    | Default: 3306                                       |
| MARIADB_DATABASE      | nein    | Default: contacts                                   |
| MARIADB_USER          | ja      | DB-Benutzer mit Schreibrechten                      |
| MARIADB_PASSWORD      | ja      | Passwort des DB-Benutzers                           |
| ACCOUNTS_CONFIG_PATH  | nein    | Default: /app/config/accounts.json                  |
| LOG_LEVEL             | nein    | Default: INFO                                       |
| MAILER_ENABLED        | nein    | Default: false, aktiviert Mailer bei true            |
| SMTP_HOST             | ja (Mailer) | SMTP-Relay-Host                                 |
| SMTP_PORT             | nein    | Default: 587                                        |
| SMTP_USER             | nein    | leer, falls Relay ohne Auth                         |
| SMTP_PASSWORD         | nein    | leer, falls Relay ohne Auth                         |
| SMTP_USE_TLS          | nein    | Default: true                                       |
| MAIL_FROM             | ja (Mailer) | Absenderadresse                                 |
| MAIL_SEND_HOUR        | nein    | Default: 7, Stunde (0-23) für täglichen Mailversand |
| WEB_URL               | nein    | Web-URL für Links in Geburtstags-Mails (z.B. https://kontakte.example.de) |
| AUTH_REMOTE_USER_HEADER | nein  | Default: Remote-User, Header-Name für Authelia-User |
| API_HOST              | nein    | Default: 0.0.0.0, Bindungs-Adresse des API-Services |
| API_PORT              | nein    | Default: 8000, Port des API-Services                |

Empfänger-Adresse für Geburtstags-Mails: `birthday_mail_to` pro Account
in `accounts.json` (keine globale Umgebungsvariable mehr nötig).

Secrets werden weiterhin als klassische Umgebungsvariablen übergeben,
mit Ausnahme der Multi-Account-Zugangsdaten, die aus `accounts.json`
gelesen werden (per Volume-Mount, nicht im Image, nicht im Git).

## 8. Container-Image

- Basis: `python:3.12-slim`.
- Python-basierter Scheduler (`src/scheduler.py`) als PID 1 im Container:
  - Keine externe Cron-Abhängigkeit (kein supercronic nötig).
  - Sync: alle 15 Minuten (`SYNC_INTERVAL_MINUTES`).
  - Mailer: täglich um `MAIL_SEND_HOUR` Uhr (falls `MAILER_ENABLED=true`).
  - Initialer Sync sofort beim Container-Start.
  - Sauberes Herunterfahren via SIGTERM/SIGINT.
- Läuft als non-root User (`syncuser`, UID 10001).
- `HEALTHCHECK` prüft die Marker-Datei `/tmp/last_sync_ok` des letzten
  erfolgreichen Sync-Laufs.

## 9. CI/CD (GitHub Actions)

Unverändert gegenüber v1:

- `build-and-push.yml`: Build und Push nach `ghcr.io` bei Push auf
  `main`, Tags `latest` und Kurz-SHA.
- Nachgelagerter `cleanup`-Job über `dataaxiom/ghcr-cleanup-action`,
  behält die letzten 4 getaggten Images, löscht ungetaggte Artefakte,
  `latest` von der Zählung ausgenommen.
- `lint.yml`: Ruff-Check (pinned `ruff==0.15.22`, alte Default-Auswahl
  `E4,E7,E9,F` via `pyproject.toml`) auf Pull Requests.

## 10. Betrieb auf der Docker-Host

- `docker-compose.yml` mountet `config/accounts.yml` read-only in den
  Container und übergibt DB- sowie SMTP-Zugangsdaten per `.env`.
- Für private GHCR-Packages weiterhin einmaliger `docker login ghcr.io`
  mit PAT (Scope `read:packages`) nötig.

## 11. Geplante spätere Erweiterungen (nicht in diesem Repo)

- **Web-Ansicht + API**: separates Container-Image (z. B. FastAPI +
  einfaches Frontend), liest ausschließlich aus derselben MariaDB,
  schreibt nicht in die `contacts`-Tabelle, um Konflikte mit dem
  Sync-Container zu vermeiden. Kann als eigenes Repository nach
  demselben Muster (Dockerfile, GitHub Actions, ghcr.io) aufgebaut
  werden.
- **Weitere Quellen**: Google Contacts und Microsoft 365 nach
  demselben Account-Muster (eigene `source`-Werte, eigene
  Sync-Strategie je Anbieter-API).
- **Mehrere Empfänger je Kontakt, Vorlauf-Erinnerungen**
  (z. B. "in 3 Tagen") sind funktional einfach nachrüstbar, aktuell
  aber nicht Teil des Scopes.
- **Contact Detail Page: UID statt DB-ID**: Der aktuelle Lookup
  `WHERE id = %s` nutzt die Auto-Increment-ID, die sich bei Re-Syncs
  ändern kann. Stabilere Alternative: `WHERE uid = %s AND account = %s`,
  da die UID aus CardDAV konstant bleibt. Erfordert Änderungen an
  Routes (`/contacts/{account}/{uid}`), Templates, Mailer-URLs und
  Schema. Details siehe `feature-contact-uid-lookup.md`.


## 12. Web-Ansicht und API (v3, im selben Repo/Image)

Ursprünglich als separates Projekt geplant, jetzt bewusst ins selbe
Repository und Image integriert, da Codebasis (Config, DB-Layer) ohnehin
geteilt wird. Getrennt ist nur die **Rolle**, in der der Container läuft.

### 12.1 Ein Image, mehrere Rollen

- Das Dockerfile bleibt unverändert eines für alle Zwecke: es enthält
  sowohl `src/sync.py`, `src/mailer.py` als auch das komplette
  `src/api/`-Package.
- `docker-compose.yml` definiert zwei Services aus demselben Image:
  - `icloud-contacts-sync`: Standard-Entrypoint, startet den
    Python-Scheduler (`scheduler.py`) mit Sync- und Mailer-Intervallen.
  - `icloud-contacts-api`: überschreibt `command` komplett mit
    `uvicorn api.main:app`, ignoriert den Scheduler-Entrypoint des Images.
- Beide Services teilen sich dieselbe MariaDB und dieselbe
  `config/accounts.json`, der API-Service greift ausschließlich lesend
  auf `contacts`, `sync_runs` zu, schreibt nichts.

### 12.2 Authelia-Integration

- Die API selbst implementiert kein Login. Sie geht davon aus, dass ein
  vorgeschalteter Reverse-Proxy (nginx/Traefik) mit Authelia via
  `auth_request` bereits authentifiziert hat und den Benutzernamen im
  Header `Remote-User` an den Container weiterreicht.
- Der Header-Name ist über `AUTH_REMOTE_USER_HEADER` konfigurierbar,
  falls dein Setup einen anderen Namen verwendet (z. B.
  `X-Forwarded-User`).
- `api/auth.py` liest diesen Header per FastAPI-`Header`-Dependency; ist
  er nicht gesetzt, antwortet die API mit 401, da dies bedeutet, dass
  der Zugriff nicht über den Authelia-geschützten Pfad erfolgte.

### 12.3 Accounts-Mapping (`authelia_user`)

- Jeder Account in `accounts.json` bekommt optional ein Feld
  `authelia_user`, das den Authelia-Benutzernamen mit dem internen
  Account-Namen verknüpft.
- Beim Request wird der Remote-User-Header gegen dieses Mapping
  aufgelöst: der Benutzer sieht ausschließlich die Kontakte seines
  eigenen Accounts, alle Queries werden serverseitig mit
  `WHERE account = %s` eingeschränkt.
- Ein zusätzliches Top-Level-Feld `admins` (Liste von
  `authelia_user`-Werten) erlaubt bestimmten Benutzern uneingeschränkten
  Zugriff auf alle Accounts, z. B. für dich als Betreiber.
- Ist ein eingeloggter Authelia-User weder gemappt noch Admin, antwortet
  die API mit 403.

### 12.4 Endpunkte

| Endpunkt | Beschreibung |
|---|---|
| `GET /` | Dashboard mit Kontaktdaten-Übersicht, letzten Sync-Status und Geburtstagen der nächsten 7 Tage (HTML) |
| `GET /search` | HTML-Übersicht mit Suchfunktion, zeigt Kontakte des zugeordneten Accounts |
| `GET /contacts/{id}` | HTML-Detailseite eines einzelnen Kontakts (Jinja2-Template) |
| `GET /api/health` | Health-Check ohne Auth-Anforderung |
| `GET /api/contacts` | Kontaktliste, Filter `q` (Freitext), Pagination `limit`/`offset` |
| `GET /api/contacts/{id}` | Einzelner Kontakt (JSON), inklusive `groups`-Feld mit zugehörigen Gruppennamen |
| `GET /api/contacts/count` | Anzahl der Kontakte des zugeordneten Accounts |
| `GET /api/contacts/birthdays/today` | Heutige Geburtstage (kontospezifisch bzw. global für Admins) |
| `GET /api/contacts/birthdays/upcoming` | Geburtstage der nächsten N Tage (Parameter `days`, Default 7) |
| `GET /api/groups` | Gruppenliste mit `member_count`, Pagination `limit`/`offset` |
| `GET /api/groups/{id}` | Einzelne Gruppe mit aufgelösten Members (Name + UID) |
| `GET /api/groups/{id}/members` | Nur Members einer Gruppe (Kontaktdaten aufgelöst) |
| `GET /api/sync-runs` | Sync-Historie (kontospezifisch bzw. global für Admins) |

### 12.5 Netzwerkkontext

- Der API-Container bindet den Port nur an `127.0.0.1:8000`, ist also
  auf der Docker-Host selbst nicht von außen erreichbar.
- Externer Zugriff läuft über deinen bestehenden Reverse-Proxy mit
  Authelia im internen Netzwerk (`deinem lokalen Netz`), der intern auf
  `127.0.0.1:8000` weiterleitet und den `Remote-User`-Header setzt.
