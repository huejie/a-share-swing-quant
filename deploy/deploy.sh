#!/usr/bin/env sh
set -eu

project_dir="${1:-/opt/a-share-swing-quant}"
cd "$project_dir"

if [ ! -f .env ]; then
  cp deploy/.env.example .env
  chmod 600 .env
fi

docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps

if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
  install -m 0644 deploy/systemd/a-share-quant-eod.service /etc/systemd/system/a-share-quant-eod.service
  install -m 0644 deploy/systemd/a-share-quant-eod.timer /etc/systemd/system/a-share-quant-eod.timer
  systemctl daemon-reload
  systemctl enable --now a-share-quant-eod.timer
fi
