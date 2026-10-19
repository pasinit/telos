#!/usr/bin/env bash
set -euo pipefail

HOST="${DEPLOY_HOST:?set DEPLOY_HOST}"
USER="${DEPLOY_USER:-root}"

echo "==> Deploying to $USER@$HOST"

ssh "$USER@$HOST" bash -s <<'REMOTE'
set -euo pipefail

cd ~/recordatio && git pull --ff-only
cd ~/telos
CADDYFILE_BEFORE=$(sha256sum Caddyfile 2>/dev/null | awk '{print $1}' || true)
git pull --ff-only
CADDYFILE_AFTER=$(sha256sum Caddyfile 2>/dev/null | awk '{print $1}' || true)

export GITHUB_TOKEN=$(grep GITHUB_TOKEN .env 2>/dev/null | cut -d= -f2 || true)
install -m 0644 deploy/telos-sandbox.apparmor /etc/apparmor.d/telos-sandbox
apparmor_parser -r /etc/apparmor.d/telos-sandbox
if ! grep -q '^COMPOSE_PROFILES=' .env; then
    export COMPOSE_PROFILES=prod
fi

if docker compose config --services | grep -qx nightly-worker; then
    install -m 0644 deploy/telos-nightly.apparmor /etc/apparmor.d/telos-nightly
    apparmor_parser -r /etc/apparmor.d/telos-nightly
fi

docker compose build
docker compose up -d --remove-orphans

# File-level bind mounts (Caddyfile → /etc/caddy/Caddyfile) lose track of
# the host file when `git pull` replaces it (different inode), so the
# running Caddy keeps serving the previous config even though the file
# on disk is current. Force-recreate Caddy whenever the file changed so
# the new mount picks up the new inode.
if [ "$CADDYFILE_BEFORE" != "$CADDYFILE_AFTER" ]; then
    echo "==> Caddyfile changed — recreating caddy to refresh bind mount"
    docker compose up -d --force-recreate caddy
fi

echo "==> Deploy complete"
docker compose ps
REMOTE
