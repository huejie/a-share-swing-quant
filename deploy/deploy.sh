#!/usr/bin/env sh
set -eu

project_dir="${1:-/opt/a-share-swing-quant}"
release_commit="${2:-}"
cd "$project_dir"

if [ ! -f .env ]; then
  cp deploy/.env.example .env
  chmod 600 .env
fi

if ! grep -q '^QUANT_ADMIN_API_KEY=' .env; then
  if command -v openssl >/dev/null 2>&1; then
    admin_key="$(openssl rand -hex 32)"
  else
    admin_key="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
  fi
  printf '\nQUANT_ADMIN_API_KEY=%s\n' "$admin_key" >> .env
fi
if ! grep -q '^BACKUP_PATH=' .env; then
  printf '%s\n' 'BACKUP_PATH=/var/backups/a-share-quant' >> .env
fi
if [ -z "$release_commit" ] && command -v git >/dev/null 2>&1 && [ -d .git ]; then
  release_commit="$(git rev-parse HEAD 2>/dev/null || true)"
fi
if printf '%s' "$release_commit" | grep -Eq '^[0-9a-f]{7,40}$'; then
  if grep -q '^QUANT_GIT_COMMIT=' .env; then
    sed -i "s/^QUANT_GIT_COMMIT=.*/QUANT_GIT_COMMIT=$release_commit/" .env
  else
    printf 'QUANT_GIT_COMMIT=%s\n' "$release_commit" >> .env
  fi
fi
admin_key="$(sed -n 's/^QUANT_ADMIN_API_KEY=//p' .env | tail -n 1)"
if [ "${#admin_key}" -lt 32 ]; then
  printf '%s\n' 'QUANT_ADMIN_API_KEY must contain at least 32 characters' >&2
  exit 1
fi

docker compose build --pull
# Existing named volumes were created by older root-running images.  Perform
# one explicit ownership migration before starting the fixed non-root runtime.
docker compose run --rm --no-deps --user 0 api sh -c 'chown -R 10001:10001 /app/data /app/backups'
docker compose up -d --remove-orphans
docker compose ps

if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
  install -d -m 0700 /etc/a-share-quant /var/backups/a-share-quant
  umask 077
  {
    printf '%s\n' 'fail' 'silent' 'show-error' 'max-time = 360' 'output = /dev/null'
    printf '%s\n' 'write-out = "status=%{http_code}\\n"'
    printf '%s\n' 'header = "Content-Type: application/json"'
    printf 'header = "X-Admin-Key: %s"\n' "$admin_key"
    printf '%s\n' 'data = "{}"' 'url = "http://127.0.0.1/api/v1/pipeline/eod"'
  } > /etc/a-share-quant/eod.curl.conf
  chmod 0600 /etc/a-share-quant/eod.curl.conf
  install -m 0644 deploy/systemd/a-share-quant-eod.service /etc/systemd/system/a-share-quant-eod.service
  install -m 0644 deploy/systemd/a-share-quant-eod.timer /etc/systemd/system/a-share-quant-eod.timer
  install -m 0644 deploy/systemd/a-share-quant-backup.service /etc/systemd/system/a-share-quant-backup.service
  install -m 0644 deploy/systemd/a-share-quant-backup.timer /etc/systemd/system/a-share-quant-backup.timer
  install -m 0644 deploy/systemd/a-share-quant-restore-verify.service /etc/systemd/system/a-share-quant-restore-verify.service
  install -m 0644 deploy/systemd/a-share-quant-restore-verify.timer /etc/systemd/system/a-share-quant-restore-verify.timer
  systemctl daemon-reload
  systemctl enable --now a-share-quant-eod.timer
  systemctl enable --now a-share-quant-backup.timer
  systemctl enable --now a-share-quant-restore-verify.timer
fi
