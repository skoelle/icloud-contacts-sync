# icloud-contacts-sync

Synct alle Kontakte mehrerer iCloud-Accounts per CardDAV Delta-Sync
(RFC 6578) automatisiert alle 15 Minuten in eine MariaDB-Datenbank
(`mariadb.internal`), plus täglichem Mailversand der heutigen
Geburtstage. Für den vollständigen technischen Hintergrund siehe
[SPEC.md](./SPEC.md).

## Voraussetzungen

- Eine oder mehrere Apple-IDs mit aktivierter Zwei-Faktor-Authentifizierung.
- Für jede Apple-ID ein app-spezifisches Passwort.
- Eine erreichbare MariaDB-Instanz mit vorbereiteter Datenbank.
- Ein SMTP-Relay (z. B. dein Mailprovider oder ein lokaler Relay) für
  den Geburtstags-Mailer.
- Docker bzw. Docker Compose auf dem Zielhost (z. B. der Docker-Host auf
  deinem Proxmox-Host).

## 1. App-spezifische Passwörter erzeugen

Für jede Apple-ID, die du syncen willst:

1. Auf `account.apple.com` mit dieser Apple-ID anmelden.
2. Zu "Anmelden & Sicherheit" → "App-spezifische Passwörter" gehen.
3. Ein neues Passwort mit sprechendem Namen erzeugen (z. B.
   `contacts-sync-debian`) und sofort sichern.

## 2. Multi-User-Konfiguration anlegen

```
cp config/accounts.yml.example config/accounts.yml
vim config/accounts.yml
```

Trage für jede Apple-ID einen Eintrag mit eindeutigem `name`,
`apple_email` und `apple_app_password` ein. Diese Datei bleibt lokal
auf dem Host, sie ist in `.gitignore` ausgeschlossen und wird nur als
Volume in den Container gemountet.

## 3. Datenbank vorbereiten

Falls Datenbank und Benutzer noch nicht existieren, führe dieses Skript einmalig aus:

```bash
mysql -u root -p < sql\db-and-user.sql
```

## 4. Umgebungsvariablen konfigurieren

```
cp .env.example .env
vim .env
```

Trage mindestens `MARIADB_USER`, `MARIADB_PASSWORD` sowie (falls du den
Mailer nutzen willst) `SMTP_HOST`, `MAIL_FROM` und `MAIL_TO` ein.

## 5. Image beziehen

```
docker login ghcr.io -u DEIN_GITHUB_USER
```

Passe in `docker-compose.yml` den Image-Namen
(`ghcr.io/DEIN_GITHUB_USER/icloud-contacts-sync:latest`) auf deinen
tatsächlichen GitHub-Namespace an.

## 6. Starten

```
docker compose up -d
```

Beim ersten Start wird für jeden Account automatisch ein vollständiger
initialer Sync ausgeführt (kein gespeicherter sync-token vorhanden).
Danach laufen alle 15 Minuten nur noch Delta-Syncs, die ausschließlich
Änderungen seit dem letzten Lauf übertragen.

## 7. Logs und Status prüfen

```
docker logs -f icloud-contacts-sync
```

Sync-Historie je Account:

```sql
SELECT account, sync_type, started_at, finished_at, status,
       contacts_upserted, contacts_deleted
FROM sync_runs
ORDER BY started_at DESC
LIMIT 20;
```

Aktueller Delta-Sync-Token je Account:

```sql
SELECT account, sync_token, updated_at FROM sync_state;
```

Versandhistorie der Geburtstagsmails:

```sql
SELECT sent_date, contacts_count, sent_at FROM birthday_mail_log
ORDER BY sent_date DESC LIMIT 10;
```

## 8. Geburtstags-Mailer

- Läuft automatisch täglich um die in `MAIL_SEND_HOUR` konfigurierte
  Stunde (Default 7 Uhr) innerhalb desselben Containers.
- Über `MAILER_ENABLED=false` lässt sich der Mailer ganz abschalten,
  ohne den Kontakt-Sync zu beeinträchtigen.
- Manueller Testlauf im laufenden Container:
  ```
  docker exec -it icloud-contacts-sync python3 /app/mailer.py
  ```
- Ein zweiter manueller Lauf am selben Tag versendet keine zweite Mail,
  solange bereits ein Eintrag in `birthday_mail_log` für heute existiert.

## 9. Lokale Entwicklung (ohne Docker)

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd src
python3 sync.py
python3 mailer.py
```

## 10. CI/CD

- Jeder Push auf `main` baut automatisch ein neues Image und pusht es
  nach `ghcr.io/<owner>/icloud-contacts-sync`.
- Ein separater Cleanup-Job behält jeweils nur die letzten 4 erfolgreich
  gebauten, getaggten Images.
- Details siehe SPEC.md, Abschnitt 9.

## Bekannte Grenzen und geplante Erweiterungen

- Delta-Sync reduziert die übertragene Datenmenge stark, ersetzt aber
  keine vollständige Historie: ein gelöschter iCloud-Kontakt wird auch
  aus MariaDB entfernt, ohne Archiv.
- Nur iCloud als Quelle, Google/Microsoft sind nicht Teil dieses Repos.
- Eine separate Web-Ansicht mit API ist als eigenständiges,
  nachgelagertes Container-Projekt geplant, das nur lesend auf dieselbe
  MariaDB zugreift (siehe SPEC.md, Abschnitt 11).


## 11. Web-Ansicht und API (interner Zugriff über Authelia)

Läuft als zweiter Service aus demselben Image, aber mit anderem
Startbefehl, siehe `docker-compose.yml` (`icloud-contacts-api`). Die API
selbst hat kein eigenes Login, sie vertraut vollständig dem
vorgeschalteten Reverse-Proxy mit Authelia.

### Voraussetzung: Reverse-Proxy mit Authelia

Dein bestehender Reverse-Proxy muss für den Pfad/Host der
Web-Ansicht einen `auth_request` gegen Authelia ausführen und danach
den authentifizierten Benutzernamen im Header `Remote-User` an
`127.0.0.1:8000` weiterreichen. Ein typischer nginx-Ausschnitt:

```
location / {
    auth_request /authelia/verify;
    auth_request_set $user $upstream_http_remote_user;
    proxy_set_header Remote-User $user;
    proxy_pass http://127.0.0.1:8000;
}
```

Falls dein Setup den Benutzernamen unter einem anderen Header liefert,
passe `AUTH_REMOTE_USER_HEADER` in der `.env` entsprechend an.

### Accounts-Mapping ergänzen

In `config/accounts.json` bekommt jeder Account zusätzlich ein Feld
`authelia_user`:

```json
{
  "accounts": [
    { "name": "markus", "apple_email": "...", "apple_app_password": "...", "authelia_user": "mmustermann" }
  ],
  "admins": ["mmustermann"]
}
```

Ein Benutzer aus `admins` sieht alle Accounts, alle anderen gemappten
Benutzer sehen ausschließlich ihren eigenen Account.

### Starten

```
docker compose up -d icloud-contacts-api
```

Der Service läuft nur an `127.0.0.1:8000`, ein direkter externer
Zugriff ohne den Reverse-Proxy ist damit nicht möglich.

### API kurz testen (lokal auf der Docker-Host, mit Header simuliert)

```
curl -H "Remote-User: mmustermann" http://127.0.0.1:8000/api/contacts
```
