#!/usr/bin/env bash
# stack-backup.sh — live, migration-ready snapshot of the 'top' self-hosted stack.
#
# Saves INTO A SINGLE FOLDER (safe to add to Syncthing), with one marker file
# `latest-backup.txt` telling you when it last ran and whether it's complete.
#
# Usage:
#   stack-backup.sh                                # full backup (auto-elevates for root-only groups)
#   stack-backup.sh --root                         # root-only groups only (used internally by auto-elevate)
#   STACK_BACKUP_NO_SUDO=1 stack-backup.sh          # agent mode: skip the 3 root-only groups
#   SNAP_DIR=/mnt/elsewhere/foo stack-backup.sh     # put the single folder somewhere else
#
# Agent/headless mode (no tty => the internal `sudo` in the auto-elevate step cannot
# prompt; run the two halves explicitly instead):
#   sudo SNAP_DIR=$HOME/backup/stack-migration bash ~/.local/bin/stack-backup.sh --root
#   STACK_BACKUP_ROOT_DONE=1 stack-backup.sh
# The first command refreshes the root-only groups (passing SNAP_DIR means it leaves the
# marker alone), the second does the user-level groups and writes the `complete` marker.
#
# Root-only groups (skipped when unprivileged): Authelia /etc/authelia,
# trawl MITM CA key, Stalwart/SnappyMail docker volumes, ntfy + uptime-kuma state,
# the cloudflare-ddns token. The root half ends by handing the whole
# folder back to the owner (root-owned leftovers made user-level refreshes skip files).
#
# Code vs backup: this script and the minipc's own scripts live in the ai-stuff repo
# (github.com/PaulDotSH/ai-stuff, minipc/), checked out at ~/ai-stuff and symlinked
# into place by minipc/link.sh. This backup holds configs, secrets and state only
# (plus the source copies it already carried, e.g. kelner); inventory.txt records the
# ai-stuff commit and any script that drifted from its symlink.
# Works correctly when run as root too: source paths are pinned to STACK_OWNER_HOME
# (default /home/minipc), NOT $HOME (sudo resets HOME to /root).

set -euo pipefail

MH="${STACK_OWNER_HOME:-/home/minipc}"            # real user home for source paths
SNAP_DIR_FROM_ENV="${SNAP_DIR:-}"                 # non-empty only when the elevating parent passed it
SNAP_DIR="${SNAP_DIR:-$MH/backup/stack-migration}"
export SNAP_DIR STACK_OWNER_HOME
NOW=$(date '+%Y-%m-%d %H:%M:%S %Z')

# ---- root-only groups -------------------------------------------------------
copy_root_only() {
  B="$1"
  mkdir -p "$B/configs/authelia" "$B/configs/trawl-proxy-ca" "$B/configs/mail"
  # legacy artifact: an old root full-run nested a copy at configs/systemd/user/user;
  # root-owned, so the user-level refresh can neither update nor delete it — drop it.
  rm -rf "$B/configs/systemd/user/user" 2>/dev/null || true
  echo "== 1/3 authelia (SSO secrets) =="
  [ -e /etc/authelia ] && cp -a /etc/authelia/. "$B/configs/authelia/" && chmod -R go-rwx "$B/configs/authelia"
  echo "== 2/3 trawl MITM CA (private key) =="
  if [ -e "$MH/services/trawl-data/proxy-ca" ]; then
    cp -a "$MH/services/trawl-data/proxy-ca/." "$B/configs/trawl-proxy-ca/" 2>/dev/null \
      || cp -a "$MH/services/trawl-data/proxy-ca" "$B/configs/trawl-proxy-ca/"
    chmod -R go-rwx "$B/configs/trawl-proxy-ca"
  fi
  echo "== 3/3 mail (stalwart + snappymail docker volumes) =="
  for v in mail_config mail_data mail_webmail; do
    src="/var/lib/docker/volumes/${v}/_data"
    [ -e "$src" ] || { echo "  (skip $v — volume not found)"; continue; }
    case "$v" in
      mail_config)  dst="$B/configs/mail/stalwart-etc";;
      mail_data)    dst="$B/configs/mail/stalwart-data";;
      mail_webmail) dst="$B/configs/mail/snappymail";;
    esac
    mkdir -p "$dst"
    rm -rf "$dst/_data"             # clean any legacy nested copy
    cp -a "$src/." "$dst/"          # CONTENTS — never nest a stale $dst/_data
  done
  # runtime wiring for the Cloudflare-inbound design (added 2026-10-20)
  copy_cfg "$B/configs/mail" /srv/stalwart.env /etc/mail-ingest.env
  mkdir -p "$B/configs/mail/cloudflared" "$B/configs/mail/mail-ingest"
  cp -a /etc/cloudflared/. "$B/configs/mail/cloudflared/" 2>/dev/null || true
  cp -a /opt/mail-ingest/. "$B/configs/mail/mail-ingest/" 2>/dev/null || true
  chmod -R go-rwx "$B/configs/mail" 2>/dev/null || true

  echo "== 4/5 app databases (new-songs sqlite + trap data) =="
  mkdir -p "$B/databases"
  # new-songs-finder sqlite (caddy-owned, running; copy -wal/-shm too if present)
  if [ -e /opt/new-songs/sqlite.db ]; then
    cp -a /opt/new-songs/sqlite.db* "$B/databases/" 2>/dev/null \
      && echo "  new-songs: $(ls -1 "$B/databases/"sqlite.db* 2>/dev/null | wc -l) file(s)"
  else
    echo "  (skip new-songs — /opt/new-songs/sqlite.db not found)"
  fi
  # trap.foarte.top data (songs.json/verses.json = its database; loaded into RAM at startup)
  if [ -e /opt/trap/data ]; then
    mkdir -p "$B/databases/trap-data"
    cp -a /opt/trap/data/. "$B/databases/trap-data/" 2>/dev/null \
      && echo "  trap-data: $(find "$B/databases/trap-data" -type f | wc -l) file(s)"
  else
    echo "  (skip trap-data — /opt/trap/data not found)"
  fi

  echo "== 5/5 ntfy + uptime-kuma state (containers run as root) =="
  mkdir -p "$B/configs/ntfy" "$B/databases/uptime-kuma"
  [ -e /etc/ntfy/server.yml ] && copy_cfg "$B/configs/ntfy" /etc/ntfy/server.yml
  # ntfy auth DB (bcrypt hashes + access tokens) + message cache; attachments (up to 512M) skipped
  for f in user.db cache.db; do
    [ -e "/var/lib/ntfy/$f" ] && cp -a "/var/lib/ntfy/$f" "$B/configs/ntfy/" 2>/dev/null
  done
  # uptime-kuma sqlite (users, monitors, notification config incl. the ntfy token)
  for f in kuma.db kuma.db-wal kuma.db-shm db-config.json; do
    [ -e "/var/lib/uptime-kuma/$f" ] && cp -a "/var/lib/uptime-kuma/$f" "$B/databases/uptime-kuma/" 2>/dev/null
  done
  chmod -R go-rwx "$B/configs/ntfy" "$B/databases/uptime-kuma" 2>/dev/null || true
  echo "  ntfy: $(find "$B/configs/ntfy" -type f | wc -l) file(s), uptime-kuma: $(find "$B/databases/uptime-kuma" -type f | wc -l) file(s)"

  echo "== chess-coach (chess db snapshot + config) =="
  mkdir -p "$B/databases/chess-coach" "$B/configs/chess-coach"
  if [ -e /var/lib/chess-coach/chess-coach.db ]; then
    tmp="/var/lib/chess-coach/.stack-backup.db"
    if runuser -u chess -- sqlite3 /var/lib/chess-coach/chess-coach.db ".backup '$tmp'" 2>/dev/null; then
      mv "$tmp" "$B/databases/chess-coach/chess-coach.db" && chmod 600 "$B/databases/chess-coach/chess-coach.db"
      echo "  chess db: $(du -h "$B/databases/chess-coach/chess-coach.db" | cut -f1)"
    else
      echo "  (skip chess db — snapshot failed)"
    fi
  else
    echo "  (skip chess db — not found)"
  fi
  copy_cfg "$B/configs/chess-coach" /etc/chess-coach/config.toml
  chmod -R go-rwx "$B/databases/chess-coach" "$B/configs/chess-coach" 2>/dev/null || true

  echo "== ai-router / Vama (config + account DB snapshot; master key deliberately NOT copied) =="
  mkdir -p "$B/configs/ai-router" "$B/databases/ai-router"
  copy_cfg "$B/configs/ai-router" /etc/ai-router/config.toml /etc/ai-router/env \
           /etc/systemd/system/ai-router.service
  if [ -e /var/lib/ai-router/ai-router.db ]; then
    tmp="/var/lib/ai-router/.stack-backup.db"
    if runuser -u ai-router -- sqlite3 /var/lib/ai-router/ai-router.db ".backup '$tmp'" 2>/dev/null; then
      mv "$tmp" "$B/databases/ai-router/ai-router.db" && chmod 600 "$B/databases/ai-router/ai-router.db"
      echo "  ai-router db: $(du -h "$B/databases/ai-router/ai-router.db" | cut -f1)"
    else
      echo "  (skip ai-router db — snapshot failed)"
    fi
  else
    echo "  (skip ai-router db — not found)"
  fi
  # the per-account browser fingerprint seed; the encrypted DB is useless without master.key,
  # which lives outside this snapshot on purpose (upstream: never with the DB)
  cp -a /var/lib/ai-router/browser-fingerprint.seed "$B/databases/ai-router/" 2>/dev/null || true
  chmod -R go-rwx "$B/databases/ai-router" "$B/configs/ai-router" 2>/dev/null || true

  echo "== lol-coach (player data + config; match data and the Riot key deliberately NOT copied) =="
  mkdir -p "$B/databases/lol-coach" "$B/configs/lol-coach"
  if [ -e /var/lib/lol-coach/players ]; then
    rsync -a --delete /var/lib/lol-coach/players/ "$B/databases/lol-coach/players/" \
      && echo "  lol-coach players: $(find "$B/databases/lol-coach/players" -type f | wc -l) file(s)"
  else
    echo "  (skip lol-coach players — not found)"
  fi
  copy_cfg "$B/configs/lol-coach" /etc/lol-coach/server.toml /etc/systemd/system/lol-coach-server.service
  chmod -R go-rwx "$B/databases/lol-coach" "$B/configs/lol-coach" 2>/dev/null || true

  echo "== root secrets (cloudflare-ddns token) =="
  mkdir -p "$B/configs/ddns"
  copy_cfg "$B/configs/ddns" /etc/cloudflare-ddns/token
  chmod 600 "$B/configs/ddns/token" 2>/dev/null || true

  # Hand everything back to the owner: modes stay go-rwx where set above, but a root-owned
  # dir inside a user-level group (seen: hermes skills, notes/research) makes the next
  # unprivileged refresh skip it silently, and Syncthing (runs as the owner) can't read it.
  chown -R "$(stat -c %u:%g "$MH")" "$B"
}

# copy_cfg <dest-dir> <src>... — for root-owned host configs. A *stale root-owned*
# copy of the same name in the snapshot blocks an overwrite from a user-level run
# (cp fails and the entry is silently skipped), so unlink the destination first:
# the snapshot dirs are user-owned, so removal always succeeds.
copy_cfg() {
  local d="$1"; shift
  local f
  for f in "$@"; do
    [ -e "$f" ] || continue
    rm -f "$d/$(basename "$f")"
    cp -p "$f" "$d/" 2>/dev/null || echo "  (copy failed: $f)"
  done
}

# ---- regular groups ---------------------------------------------------------
copy_unprivileged() {
  B="$1"
  mkdir -p "$B"/{configs/{caddy,qbittorrent,syncthing,kelner,microbin,trawl,homepage,signal-cli,arrs,stalwart,stalwart-webmail,ddns,systemd,hermes},notes}

  echo "== caddy ==";        copy_cfg "$B/configs/caddy" /etc/caddy/Caddyfile
  echo "== qbittorrent ==";  cp -p "$MH/.config/qBittorrent/qBittorrent.conf" "$B/configs/qbittorrent/" 2>/dev/null || echo "  (skip qbt)"
  echo "== syncthing (config+keys, no index cache) =="
  if [ -e "$MH/.local/state/syncthing" ]; then
    cp -r "$MH/.local/state/syncthing" "$B/configs/syncthing/" 2>/dev/null || true
    rm -rf "$B/configs/syncthing/syncthing/index-v2" "$B/configs/syncthing/index-v2" 2>/dev/null || true
  fi
  echo "== kelner source =="
  if [ -e /opt/kelner ]; then
    cp -r /opt/kelner/src /opt/kelner/templates /opt/kelner/static /opt/kelner/migrations \
          /opt/kelner/Cargo.toml /opt/kelner/Cargo.lock /opt/kelner/README.md /opt/kelner/DEPLOY.md "$B/configs/kelner/" 2>/dev/null || true
  fi
  echo "== microbin ==";     [ -e /var/lib/microbin ] && cp -r /var/lib/microbin "$B/configs/microbin/" 2>/dev/null || true
  echo "== trawl data (CA key added by root mode) =="
  [ -e "$MH/services/trawl-data" ] && find "$MH/services/trawl-data" -maxdepth 1 ! -name proxy-ca ! -name . -exec cp -r {} "$B/configs/trawl/" \; 2>/dev/null || true
  echo "== homepage (foarte.top / + /me pages) + cv (/srv/foarte-cv) =="; mkdir -p "$B/configs/homepage"/{public,private} "$B/configs/cv" && { cp -p /srv/foarte-home/public/index.html /srv/foarte-home/public/sitemap.xml "$B/configs/homepage/public/" && cp -p /srv/foarte-home/private/index.html "$B/configs/homepage/private/" && cp -p /srv/foarte-cv/index.html "$B/configs/cv/"; } 2>/dev/null || echo "  (skip homepage — /srv/foarte-home not readable)"
  echo "== signal-cli ==";   [ -e "$MH/.local/share/signal-cli" ] && cp -r "$MH/.local/share/signal-cli" "$B/configs/signal-cli/" 2>/dev/null || true
  echo "== ddns + systemd bits =="
  copy_cfg "$B/configs/ddns" /usr/local/bin/cf-ddns.py
  copy_cfg "$B/configs/systemd" /etc/systemd/zram-generator.conf \
            /etc/systemd/system/qbittorrent-nox@minipc.service.d/override.conf \
            /usr/local/bin/mail-smtp-on /usr/local/bin/mail-smtp-off \
            /etc/systemd/system/cloudflare-ddns.service /etc/systemd/system/cloudflare-ddns.timer \
            /etc/systemd/system/new-songs.service /etc/systemd/system/trap-foarte-top.service \
            /etc/systemd/system/septica.service \
            /etc/systemd/system/authelia.service \
            /usr/local/bin/cf-origin-guard /etc/systemd/system/cf-origin-guard.service \
            /etc/systemd/system/cf-origin-guard.timer \
            /etc/pacman.d/hooks/seerr-restart.hook \
            /etc/systemd/system/chess-coach.service /etc/systemd/system/chess-coach-backup.service \
            /etc/systemd/system/chess-coach-backup.timer \
            /usr/local/lib/chess-coach/backup.sh \
            /etc/systemd/system/stalwart-mail.service /etc/systemd/system/mail-ingest.service \
            /etc/systemd/system/cloudflared.service /usr/lib/systemd/system/slskd.service \
            /home/minipc/.local/bin/cf-edge-rules
  # `/.` form: copy the CONTENTS, never nest. A plain `cp -r src dst/` targets
  # dst/<basename> when it exists, so a stray root-owned dst/user (legacy artifact,
  # cleaned in copy_root_only) made every user-level refresh fail silently and the
  # snapshot's units went stale — new units (jocuri, opencode2api) were missing.
  if [ -d "$MH/.config/systemd/user" ]; then
    mkdir -p "$B/configs/systemd/user"
    cp -r "$MH/.config/systemd/user/." "$B/configs/systemd/user/" 2>/dev/null || true
  fi
  echo "== jocuri env (runtime config; source itself is the GitHub checkout) =="
  mkdir -p "$B/configs/jocuri"
  cp -p "$MH/services/jocuri-foarte-top/jocuri.env" "$B/configs/jocuri/" 2>/dev/null || echo "  (skip jocuri.env)"

  echo "== arrs (configs + DBs only) =="
  for app in sonarr radarr lidarr prowlarr bazarr seerr jackett slskd profilarr; do
    src="/var/lib/$app"; dst="$B/configs/arrs/$app"
    [ -d "$src" ] || { echo "  (skip $app)"; continue; }
    mkdir -p "$dst"
    find "$src" -maxdepth 2 -type f \( -name 'config.xml' -o -name '*.db' -o -name '*.sqlite3' \
      -o -name '*.json' -o -name '*.yml' -o -name '*.yaml' -o -name '*.ini' \) \
      -not -path '*/MediaCover/*' -exec cp --parents {} "$dst" \; 2>/dev/null || true
    if [ "$app" = "slskd" ]; then
      # config + state only; downloads/incomplete are payload, not config
      mkdir -p "$dst/.local/share/slskd"
      cp -p "$src/.local/share/slskd/slskd.yml" "$dst/.local/share/slskd/" 2>/dev/null || true
      cp -r "$src/.local/share/slskd/data" "$dst/.local/share/slskd/" 2>/dev/null || true
      rm -rf "$dst/.local/share/slskd/downloads" "$dst/.local/share/slskd/incomplete"
    else
      [ -d "$src/.local" ] && cp -r "$src/.local" "$dst" 2>/dev/null || true
    fi
    echo "  $app: $(find "$dst" -type f 2>/dev/null | wc -l) files"
  done
  # cleanuparr keeps nothing in /var/lib/cleanuparr: the AUR unit writes to
  # /usr/lib/cleanuparr/config (DBs + DataProtection-Keys, which decrypt its stored API keys)
  if [ -d /usr/lib/cleanuparr/config ]; then
    mkdir -p "$B/configs/arrs/cleanuparr"
    rsync -a --delete --exclude 'logs/' /usr/lib/cleanuparr/config/ "$B/configs/arrs/cleanuparr/" || true
    chmod -R go-rwx "$B/configs/arrs/cleanuparr" 2>/dev/null || true
    echo "  cleanuparr: $(find "$B/configs/arrs/cleanuparr" -type f | wc -l) files"
  fi

  echo "== jellyfin (state only; no cache/metadata/backups) =="
  if [ -e /var/lib/jellyfin ]; then
    mkdir -p "$B/configs/jellyfin"
    for f in system.xml network.xml encoding.xml branding.xml database.xml logging.default.json; do
      cp /var/lib/jellyfin/$f "$B/configs/jellyfin/" 2>/dev/null || true
    done
    cp -r /var/lib/jellyfin/ScheduledTasks /var/lib/jellyfin/disabled-plugins "$B/configs/jellyfin/" 2>/dev/null || true
    cp -r /var/lib/jellyfin/data "$B/configs/jellyfin/" 2>/dev/null || true
    rm -rf "$B/configs/jellyfin/data/metadata" "$B/configs/jellyfin/data/data/backups" \
           "$B/configs/jellyfin/data/data/trickplay" "$B/configs/jellyfin/data/data/library.db.old" \
           "$B/configs/jellyfin/data/data/splashscreen.png" 2>/dev/null || true
  fi

  echo "== hermes (config/skills/plugins/cron; no .hub cache) =="
  mkdir -p "$B/configs/hermes"
  cp "$MH/.hermes/config.yaml" "$B/configs/hermes/" 2>/dev/null || true
  cp "$MH/.hermes/.env" "$B/configs/hermes/" 2>/dev/null || true
  cp -r "$MH/.hermes/skills" "$MH/.hermes/plugins" "$MH/.hermes/cron" "$B/configs/hermes/" 2>/dev/null || true
  rm -rf "$B/configs/hermes/skills/.hub" 2>/dev/null || true
  # identity, memory and the credential files other scripts read (cal-*, ntfy-push, cf-*)
  ( cd "$MH/.hermes" && cp -p SOUL.md auth.json channel_directory.json cf-waf-token .abbott_sonarr_key \
      ./*creds*.txt ./*token*.txt "$B/configs/hermes/" ) 2>/dev/null || true
  cp -r "$MH/.hermes/memories" "$MH/.hermes/vault" "$MH/.hermes/scripts" "$MH/.hermes/hooks" \
        "$B/configs/hermes/" 2>/dev/null || true
  for db in memory_store kanban projects shared-state; do
    [ -e "$MH/.hermes/$db.db" ] || continue
    sqlite3 "$MH/.hermes/$db.db" ".backup '$B/configs/hermes/$db.db'" 2>/dev/null \
      || echo "  (WARN: hermes $db.db snapshot failed)"
  done
  chmod -R go-rwx "$B/configs/hermes" 2>/dev/null || true
  echo "  hermes: $(find "$B/configs/hermes" -type f 2>/dev/null | wc -l) file(s)"

  echo "== opencode + opencode2api (config + auth) =="
  mkdir -p "$B/configs/opencode" "$B/configs/opencode2api"
  ( cd "$MH/.config/opencode" && cp -p opencode.jsonc package.json service.json "$B/configs/opencode/" ) 2>/dev/null || true
  cp -p "$MH/.local/share/opencode/auth.json" "$B/configs/opencode/" 2>/dev/null || true
  if [ -e "$MH/.local/share/opencode/opencode.db" ]; then
    sqlite3 "$MH/.local/share/opencode/opencode.db" ".backup '$B/configs/opencode/opencode.db'" 2>/dev/null \
      || echo "  (WARN: opencode.db snapshot failed)"
  fi
  cp -p "$MH/.config/opencode2api"/config.json* "$B/configs/opencode2api/" 2>/dev/null || true
  chmod -R go-rwx "$B/configs/opencode" "$B/configs/opencode2api" 2>/dev/null || true

  echo "== homepage dashboard (docker, /opt/homepage/config) + jellyfin compose =="
  if [ -d /opt/homepage/config ]; then
    rsync -a --delete --exclude 'logs/' /opt/homepage/config/ "$B/configs/homepage/config/" || echo "  (WARN: homepage config copy incomplete)"
  fi
  mkdir -p "$B/configs/jellyfin"
  copy_cfg "$B/configs/jellyfin" /opt/jellyfin/docker-compose.yml

  echo "== local-only services: state/config/secrets (their code is in ai-stuff/minipc) =="
  # every ~/services dir without a git remote, minus ones handled above and clef-classify (models)
  for d in "$MH/services"/*/; do
    s=$(basename "$d")
    case "$s" in rustical|silverbullet|stalwart|trawl-data|clef-classify) continue;; esac
    [ -e "$d/.git" ] && continue
    mkdir -p "$B/configs/local-services/$s"
    rsync -a --delete --exclude '*.py' --exclude '__pycache__/' --exclude '*.log' --exclude '*.log.*' \
          --exclude '*.bak*' --exclude 'state/backup-*/' "$d" "$B/configs/local-services/$s/" || true
    echo "  $s: $(du -sh "$B/configs/local-services/$s" 2>/dev/null | cut -f1)"
  done
  chmod -R go-rwx "$B/configs/local-services" 2>/dev/null || true

  echo "== all host systemd units (regular files; enablement is listed in inventory.txt) =="
  mkdir -p "$B/configs/systemd/etc-system"
  rsync -aq --delete --no-links --exclude '*.wants/' --exclude '*.requires/' \
        /etc/systemd/system/ "$B/configs/systemd/etc-system/" 2>/dev/null \
    || echo "  (WARN: some units unreadable)"
  echo "  units: $(find "$B/configs/systemd/etc-system" -type f | wc -l) file(s)"

  echo "== docker definitions (inspect = env, ports, mounts; enough to recreate) =="
  mkdir -p "$B/configs/docker"
  if docker info >/dev/null 2>&1; then
    docker ps -aq | xargs -r docker inspect > "$B/configs/docker/containers.json" 2>/dev/null || true
    docker volume ls -q | xargs -r docker volume inspect > "$B/configs/docker/volumes.json" 2>/dev/null || true
    docker network ls -q | xargs -r docker network inspect > "$B/configs/docker/networks.json" 2>/dev/null || true
    chmod -R go-rwx "$B/configs/docker"   # container env holds secrets
    echo "  containers: $(docker ps -aq | wc -l)"
  else
    echo "  (skip docker — daemon not reachable)"
  fi

  echo "== stalwart stack (unit source, README, nginx, worker) =="
  [ -d "$MH/services/stalwart" ] && cp -r "$MH/services/stalwart/." "$B/configs/stalwart/" 2>/dev/null || true

  echo "== silverbullet (accounts/server config + notes; .git history skipped) =="
  if [ -e "$MH/services/silverbullet" ]; then
    mkdir -p "$B/configs/silverbullet" "$B/silverbullet-notes"
    cp -p "$MH/services/silverbullet"/users.json "$MH/services/silverbullet"/spaces.json "$MH/services/silverbullet"/server.json \
          "$B/configs/silverbullet/" 2>/dev/null || true
    chmod -R go-rwx "$B/configs/silverbullet" 2>/dev/null || true
    if [ -d "$MH/services/silverbullet/spaces" ]; then
      ( cd "$MH/services/silverbullet/spaces" && find . -path '*/.git' -prune -o -type f -print0 \
          | xargs -0 -I{} cp --parents -p {} "$B/silverbullet-notes/" ) 2>/dev/null || true
      echo "  notes: $(find "$B/silverbullet-notes" -type f 2>/dev/null | wc -l) file(s)"
    fi
  else
    echo "  (skip silverbullet — $MH/services/silverbullet not found)"
  fi

  echo "== rustical calendar (sqlite snapshot + service docs) =="
  if [ -d "$MH/services/rustical/data" ]; then
    mkdir -p "$B/configs/rustical"
    cp -p "$MH/services/rustical"/*.sh "$MH/services/rustical"/*.md "$B/configs/rustical/" 2>/dev/null || true
    # It runs WAL mode: db.sqlite3 alone is a 4 KB header whose data lives in the
    # -wal file, so a plain cp backs up NOTHING. Never copy while running — instead
    # let sqlite merge the log into a standalone snapshot (sqlite3 .backup is
    # crash-safe against a live writer).
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$MH/services/rustical/data/db.sqlite3" ".backup '$B/configs/rustical/rustical-db.sqlite3'" 2>/dev/null \
        || echo "  (WARN: sqlite3 .backup failed — calendar NOT captured)"
      chmod 600 "$B/configs/rustical/rustical-db.sqlite3" 2>/dev/null || true
      echo "  db: $(du -h "$B/configs/rustical/rustical-db.sqlite3" 2>/dev/null | cut -f1) (integrity: $(sqlite3 "$B/configs/rustical/rustical-db.sqlite3" 'PRAGMA integrity_check;' 2>/dev/null | head -1))"
    else
      echo "  (WARN: sqlite3 CLI missing — cannot snapshot the calendar db safely)"
    fi
  else
    echo "  (skip rustical — $MH/services/rustical not found)"
  fi

  echo "== notes (Hermes findings + research reports) =="
  cp -r "$MH/MIGRATE"/. "$B/notes/" 2>/dev/null || true
  cp -p "$MH/backup"/*.md "$B/notes/" 2>/dev/null || true
  [ -d "$MH/temp-hermes/research" ] && { mkdir -p "$B/notes/research"; cp -r "$MH/temp-hermes/research"/. "$B/notes/research/" 2>/dev/null || true; } || true
}

inventory() {
  B="$1"
  { echo "SNAPSHOT $(date -Iseconds)"; echo; echo '== listening =='; ss -tlnp 2>/dev/null;
    echo; echo '== docker =='; docker ps -a --format '{{.Names}} | {{.Image}} | {{.Status}} | {{.Ports}}' 2>/dev/null;
    echo; echo '== stack units =='; systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null \
      | awk '{print $1}' | grep -ivE 'systemd|dbus|logind|udev|NetworkManager|getty|journal|user@|tmp|haveged|chrono|pkgfile|reflector|Pacman|bluetoo';
    echo; echo '== enabled units (system) =='; systemctl list-unit-files --state=enabled --no-legend --no-pager 2>/dev/null | awk '{print $1}';
    echo; echo '== enabled units (user) =='; systemctl --user list-unit-files --state=enabled --no-legend --no-pager 2>/dev/null | awk '{print $1}';
    echo; echo '== code (ai-stuff/minipc) =='
    if [ -d "$MH/ai-stuff/.git" ]; then
      echo "commit $(git -C "$MH/ai-stuff" rev-parse --short HEAD 2>/dev/null) $(git -C "$MH/ai-stuff" status --porcelain -- minipc 2>/dev/null | wc -l) uncommitted file(s) in minipc/"
      bash "$MH/ai-stuff/minipc/link.sh" --check 2>&1 && echo "all scripts linked"
    else
      echo "MISSING: $MH/ai-stuff checkout"
    fi; } \
    > "$B/inventory.txt" || true
  grep -E '^(DRIFTED|UNLINKED|MISSING)' "$B/inventory.txt" | sed 's/^/  WARN code: /' || true
}

seed_readme() {
  B="$1"
  [ -e "$B/README.md" ] && return 0
  local old
  old=$(find "$MH/backup" -maxdepth 1 -type d -name 'stack-migration-[0-9]*' 2>/dev/null -printf '%T@ %p\n' | sort -rn | head -1 | cut -d' ' -f2-)
  [ -n "$old" ] && [ -e "$old/README.md" ] && cp -p "$old/README.md" "$B/README.md" && echo "seeded README from $old"
}

write_marker() {
  local status="$1"
  {
    printf 'last backup: %s  (%s)\n' "$NOW" "$status"
    printf 'single live backup folder — safe to add to Syncthing. Refresh anytime with: stack-backup.sh\n'
  } > "$SNAP_DIR/latest-backup.txt"
}

# ---- main -------------------------------------------------------------------
if [ "${1:-}" = "--root" ]; then
  copy_root_only "$SNAP_DIR"
  # Standalone run (no SNAP_DIR inherited from the elevating parent): the caller
  # ran only the privileged half, so refresh the marker instead of leaving a
  # stale "partial (elevation failed)" verdict behind.
  [ -z "$SNAP_DIR_FROM_ENV" ] && write_marker "root-only groups refreshed only — run stack-backup.sh without --root for the user-level groups"
  echo "root-only groups done → $SNAP_DIR"
  exit 0
fi

echo "==> target: $SNAP_DIR"
copy_unprivileged "$SNAP_DIR"
inventory "$SNAP_DIR"
seed_readme "$SNAP_DIR"

if [ "$(id -u)" -eq 0 ]; then
  echo "running as root; doing root-only groups too"
  copy_root_only "$SNAP_DIR"
  write_marker "complete"
else
  if [ "${STACK_BACKUP_NO_SUDO:-0}" = 1 ]; then
    write_marker "partial (root-only groups missing — authelia/trawl-ca/mail)"
    echo "HINT: root-only groups missing. For a complete backup run:"
    echo "  sudo bash $0"
  elif [ "${STACK_BACKUP_ROOT_DONE:-0}" = 1 ]; then
    write_marker "complete"
    echo "root-only groups were refreshed by a separate sudo run (STACK_BACKUP_ROOT_DONE=1)"
  else
    echo "==> elevating for root-only groups (authelia, trawl CA, mail)..."
    if sudo SNAP_DIR="$SNAP_DIR" bash "$0" --root; then
      write_marker "complete"
    else
      write_marker "partial (elevation failed — root-only groups missing)"
      echo "WARN: elevation failed; root-only groups not backed up."
    fi
  fi
fi
cat "$SNAP_DIR/latest-backup.txt"
echo "Done: $(du -sh "$SNAP_DIR" 2>/dev/null | cut -f1)"
