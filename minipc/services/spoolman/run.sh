#!/usr/bin/env bash
# Recreate the Spoolman container (filament spool inventory) on this box.
# Usage: ./run.sh          (recreate, keeps the SQLite data in ./data)
#
# LAN only: published on every interface at :7912 with no login, so it must not
# get a Caddy/cloudflared route. Clients: OrcaSlicer's Spoolio plugin and the
# 3D-printing scripts on the PC, both at http://192.168.0.226:7912.
# Data: ./data/spoolman.db (stack-backup.sh snapshots it) plus Spoolman's own
# nightly copies in ./data/backups.
set -euo pipefail
DATA_DIR=/home/minipc/services/spoolman/data
IMAGE=ghcr.io/donkie/spoolman:0.27.1
mkdir -p "$DATA_DIR"
docker rm -f spoolman >/dev/null 2>&1 || true
docker run -d --name spoolman \
  --restart unless-stopped \
  -p 7912:8000 \
  -v "$DATA_DIR":/home/app/.local/share/spoolman \
  -e TZ=Europe/Bucharest \
  -e PUID=1000 -e PGID=1000 \
  "$IMAGE"
sleep 5
docker ps --filter name=spoolman --format '{{.Names}} {{.Status}} {{.Ports}}'
