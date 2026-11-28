FROM python:3.11-slim

ARG UID=1000
ARG GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    GW_ROOT=/data/gateway \
    WEB_DATA=/data/web \
    GW_BIN=/app/bin/workbuddy-gateway

RUN groupadd -g "${GID}" wb \
 && useradd -u "${UID}" -g "${GID}" -m -s /bin/sh wb \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata gosu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-permissions.txt /app/requirements-permissions.txt
RUN pip install --no-cache-dir -r /app/requirements-permissions.txt
COPY bin/workbuddy-gateway-manual /app/bin/workbuddy-gateway
COPY config/config.json.example /app/config/config.json.example
COPY app/ /app/app/
COPY scripts/usage_etl/ /app/usage_etl/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod 755 /app/bin/workbuddy-gateway /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /data/gateway /data/web \
 && chown -R "${UID}:${GID}" /app /data \
 && chmod 700 /data/gateway /data/web

EXPOSE 8080 8317 8318
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "/app/app/boot.py"]
