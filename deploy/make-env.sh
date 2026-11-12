#!/usr/bin/env bash
# Runs ON the server: writes /opt/postiz/.env with fresh random secrets, only if it doesn't exist yet.
# Usage: sudo bash make-env.sh [domain]      (default: social.muhammadmuneeb.tech)
# Platform developer keys do NOT go here: Shipcast Control writes them to /opt/postiz/keys.env, and
# docker-compose loads both files.
set -euo pipefail
DOMAIN="${1:-social.muhammadmuneeb.tech}"
cd /opt/postiz
[ -e .env ] && { echo ".env exists, leaving it alone"; exit 0; }
r() { openssl rand -hex "$1"; }
DB=$(r 24); TDB=$(r 24)
umask 077
cat > .env <<EOF
# --- compose substitution
POSTIZ_DB_PASSWORD=$DB
TEMPORAL_DB_PASSWORD=$TDB

# --- core
MAIN_URL=https://$DOMAIN
FRONTEND_URL=https://$DOMAIN
NEXT_PUBLIC_BACKEND_URL=https://$DOMAIN/api
BACKEND_INTERNAL_URL=http://localhost:3000
JWT_SECRET=$(r 48)
DATABASE_URL=postgresql://postiz-user:$DB@postiz-postgres:5432/postiz-db-local
REDIS_URL=redis://postiz-redis:6379
TEMPORAL_ADDRESS=temporal:7233
IS_GENERAL=true
# open sign-up: every customer gets their own workspace (no email provider = accounts are active at once)
DISABLE_REGISTRATION=false
RUN_CRON=true
API_LIMIT=30
NX_ADD_PLUGINS=false

# --- storage (served publicly at /uploads, which Instagram needs)
STORAGE_PROVIDER=local
UPLOAD_DIRECTORY=/uploads
NEXT_PUBLIC_UPLOAD_DIRECTORY=/uploads
MASTODON_URL=https://mastodon.social
# Meta no longer offers Instagram Login to new apps; Instagram connects through Facebook instead
HIDDEN_PROVIDERS=instagram-standalone
EOF
[ -e keys.env ] || { : > keys.env; chown hub:hub keys.env 2>/dev/null || true; chmod 600 keys.env; }
echo "wrote /opt/postiz/.env (mode 600)"
