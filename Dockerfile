FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/
COPY sql/ /app/sql/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN mkdir -p /app/config \
    && useradd -m -u 10001 syncuser \
    && chown -R syncuser:syncuser /app
USER syncuser

HEALTHCHECK --interval=5m --timeout=10s --start-period=30s \
    CMD test -f /tmp/last_sync_ok || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
