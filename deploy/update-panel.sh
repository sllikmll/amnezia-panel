#!/bin/sh
set -eu

IMAGE="${PANEL_IMAGE:-ghcr.io/sllikmll/amnezia-panel:latest}"
NAME="${PANEL_CONTAINER_NAME:-awg-panel}"
PORT="${PANEL_PORT:-8888}"
HOST_DATA_DIR="${PANEL_HOST_DATA_DIR:-${AWG_DATA_DIR:-/data/awg}}"
ENDPOINT="${AWG_ENDPOINT:-vpn.example.com:51820}"
DB_PATH="${AWG_DB_PATH:-/data/panel.db}"
CONFIG_PATH="${AWG_CONFIG_PATH:-/data/awg0.conf}"
TARGET="${PANEL_TARGET_VERSION:-latest}"
UPDATER_NAME="${NAME}-updater-$(date +%s)"

if [ "${PANEL_UPDATER_CHILD:-0}" != "1" ]; then
  echo "[update] scheduling helper ${UPDATER_NAME} for ${NAME} -> ${TARGET} (${IMAGE})"
  docker pull "$IMAGE"
  docker run -d \
    --name "$UPDATER_NAME" \
    --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$HOST_DATA_DIR:/data" \
    -e PANEL_UPDATER_CHILD=1 \
    -e PANEL_IMAGE="$IMAGE" \
    -e PANEL_CONTAINER_NAME="$NAME" \
    -e PANEL_PORT="$PORT" \
    -e PANEL_HOST_DATA_DIR="$HOST_DATA_DIR" \
    -e PANEL_TARGET_VERSION="$TARGET" \
    -e PANEL_ADMIN_USER="${PANEL_ADMIN_USER:-admin}" \
    -e PANEL_ADMIN_PASSWORD="${PANEL_ADMIN_PASSWORD:-}" \
    -e PANEL_SECRET_KEY="${PANEL_SECRET_KEY:-}" \
    -e PANEL_ENCRYPTION_KEY="${PANEL_ENCRYPTION_KEY:-}" \
    -e PANEL_REPO="${PANEL_REPO:-sllikmll/amnezia-panel}" \
    -e AWG_DB_PATH="$DB_PATH" \
    -e AWG_CONFIG_PATH="$CONFIG_PATH" \
    -e AWG_ENDPOINT="$ENDPOINT" \
    -e AWG_LISTEN_PORT="${AWG_LISTEN_PORT:-51820}" \
    --entrypoint /bin/sh \
    "$IMAGE" \
    -lc 'sleep 3; PANEL_UPDATER_CHILD=1 /usr/local/bin/update-panel'
  echo "[update] helper started"
  exit 0
fi

echo "[update-child] target=${TARGET} image=${IMAGE} container=${NAME}"
echo "[update-child] pulling image"
docker pull "$IMAGE"

RESTART_POLICY="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$NAME" 2>/dev/null || echo unless-stopped)"
if [ -z "$RESTART_POLICY" ] || [ "$RESTART_POLICY" = "no" ]; then RESTART_POLICY="unless-stopped"; fi

echo "[update-child] removing old container ${NAME}"
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "[update-child] starting new container"
docker run -d \
  --name "$NAME" \
  --restart "$RESTART_POLICY" \
  --network host \
  --cap-add NET_ADMIN \
  -v "$HOST_DATA_DIR:/data" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e PANEL_ADMIN_USER="${PANEL_ADMIN_USER:-admin}" \
  -e PANEL_ADMIN_PASSWORD="${PANEL_ADMIN_PASSWORD:-}" \
  -e PANEL_SECRET_KEY="${PANEL_SECRET_KEY:-}" \
  -e PANEL_ENCRYPTION_KEY="${PANEL_ENCRYPTION_KEY:-}" \
  -e PANEL_PORT="$PORT" \
  -e PANEL_VERSION="${PANEL_TARGET_VERSION#v}" \
  -e PANEL_REPO="${PANEL_REPO:-sllikmll/amnezia-panel}" \
  -e PANEL_IMAGE="$IMAGE" \
  -e PANEL_CONTAINER_NAME="$NAME" \
  -e PANEL_UPDATE_COMMAND="/usr/local/bin/update-panel" \
  -e PANEL_HOST_DATA_DIR="$HOST_DATA_DIR" \
  -e AWG_DATA_DIR=/data \
  -e AWG_DB_PATH="$DB_PATH" \
  -e AWG_CONFIG_PATH="$CONFIG_PATH" \
  -e AWG_ENDPOINT="$ENDPOINT" \
  -e AWG_LISTEN_PORT="${AWG_LISTEN_PORT:-51820}" \
  "$IMAGE"

echo "[update-child] done"
