#!/bin/sh
set -e

MAIL_HOUR="${MAIL_SEND_HOUR:-7}"

echo "Starte icloud-contacts-sync Container"
echo "Sync-Schedule: */15 * * * * (alle 15 Minuten, Delta-Sync)"
echo "Mailer-Schedule: taeglich um ${MAIL_HOUR} Uhr (falls MAILER_ENABLED=true)"

# Crontab wird zur Laufzeit generiert, damit MAIL_SEND_HOUR konfigurierbar bleibt.
cat > /etc/crontabs/app-crontab <<EOF
*/15 * * * * /usr/local/bin/run-sync.sh >> /proc/1/fd/1 2>> /proc/1/fd/2
0 ${MAIL_HOUR} * * * /usr/local/bin/run-mailer.sh >> /proc/1/fd/1 2>> /proc/1/fd/2
EOF

/usr/local/bin/run-sync.sh || echo "Initialer Sync fehlgeschlagen, Cron laeuft trotzdem weiter"

exec supercronic -json /etc/crontabs/app-crontab
