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
