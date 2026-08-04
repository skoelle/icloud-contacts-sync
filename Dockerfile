FROM python:3.12-slim

ARG SUPERCRONIC_VERSION=v0.2.48
ARG SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64
ARG SUPERCRONIC_SHA1SUM=016b7c9aebfc8d9fd9526e8ba33b191fc524485f

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSLO "$SUPERCRONIC_URL" \
    && echo "${SUPERCRONIC_SHA1SUM}  supercronic-linux-amd64" | sha1sum -c - \
    && chmod +x supercronic-linux-amd64 \
    && mv supercronic-linux-amd64 /usr/local/bin/supercronic \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Enthält sowohl den Sync/Mailer-Code als auch die api/-App.
# Welcher Teil tatsächlich läuft, entscheidet der Command in docker-compose.yml,
# nicht das Image selbst -- ein Image, drei mögliche Rollen (sync, mailer, api).
COPY src/ /app/
COPY docker/run-sync.sh /usr/local/bin/run-sync.sh
COPY docker/run-mailer.sh /usr/local/bin/run-mailer.sh
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/crontab.default /etc/crontabs/app-crontab

RUN chmod +x /usr/local/bin/run-sync.sh /usr/local/bin/run-mailer.sh /usr/local/bin/entrypoint.sh

RUN mkdir -p /app/config \
    && useradd -m -u 10001 syncuser \
    && chown -R syncuser:syncuser /app /etc/crontabs
USER syncuser

HEALTHCHECK --interval=5m --timeout=10s --start-period=30s \
    CMD test -f /tmp/last_sync_ok || exit 1

# Default-Entrypoint startet den Sync+Mailer-Cron-Modus.
# Der Web/API-Service in docker-compose.yml überschreibt "command" komplett
# mit uvicorn und läuft damit im selben Image in einer anderen Rolle.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
