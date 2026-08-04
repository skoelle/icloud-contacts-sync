# config/accounts.json

Diese Datei enthält alle Apple-ID-Zugangsdaten sowie das Mapping auf
Authelia-Benutzernamen. Sie liegt bewusst außerhalb von Git (siehe
`.gitignore`) und wird als Volume in beide Container (Sync und
Web/API) gemountet.

Felder pro Account:

- `name`: interner, eindeutiger Account-Bezeichner (Spalte `account`
  in MariaDB).
- `apple_email` / `apple_app_password`: CardDAV-Zugangsdaten, nur vom
  Sync-Container genutzt.
- `authelia_user`: Benutzername, wie ihn Authelia im
  `Remote-User`-Header an die Web-Ansicht/API durchreicht. Dieser Wert
  bestimmt, welchen Account ein eingeloggter Benutzer in der
  Web-Ansicht sieht.

Optionales Feld `admins` (Liste von `authelia_user`-Werten): diese
Benutzer sehen in der Web-Ansicht/API die Kontakte aller Accounts,
nicht nur ihren eigenen.

Kopiere `accounts.json.example` nach `accounts.json` und trage echte
Werte ein:

```
cp accounts.json.example accounts.json
vim accounts.json
```
