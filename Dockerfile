FROM alpine:3.20

ARG AWG_TOOLS_VERSION=v3.0.20260805

RUN apk add --no-cache \
    python3 \
    py3-pip \
    bash \
    curl \
    ca-certificates \
    unzip \
    sqlite \
    wireguard-tools \
    docker-cli \
    && curl -fsSL -o /tmp/awg-tools.zip \
       https://github.com/amnezia-vpn/amneziawg-tools/releases/download/${AWG_TOOLS_VERSION}/alpine-3.19-amneziawg-tools.zip \
    && cd /tmp && unzip -o awg-tools.zip \
    && chmod +x /tmp/alpine-3.19-amneziawg-tools/awg /tmp/alpine-3.19-amneziawg-tools/awg-quick \
    && mv /tmp/alpine-3.19-amneziawg-tools/awg /usr/local/bin/awg \
    && mv /tmp/alpine-3.19-amneziawg-tools/awg-quick /usr/local/bin/awg-quick \
    && rm -rf /tmp/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY backend/main.py backend/multi_server.py backend/crypto.py backend/aggregate.py backend/discover.py backend/notify.py backend/realtime.py backend/update_utils.py ./
COPY frontend/index.html /app/static/index.html
COPY deploy/update-panel.sh /usr/local/bin/update-panel
RUN chmod +x /usr/local/bin/update-panel

ENV PANEL_PORT=8080 \
    PANEL_VERSION=1.1.7 \
    PANEL_REPO=sllikmll/amnezia-panel \
    PANEL_IMAGE=ghcr.io/sllikmll/amnezia-panel:latest \
    PANEL_CONTAINER_NAME=awg-panel \
    PANEL_UPDATE_COMMAND=/usr/local/bin/update-panel \
    PANEL_HOST_DATA_DIR=/data \
    AWG_DATA_DIR=/data \
    AWG_DB_PATH=/data/panel.db \
    AWG_CONFIG_PATH=/data/awg0.conf

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -fsS http://localhost:${PANEL_PORT}/ >/dev/null || exit 1

CMD ["python3", "main.py"]
