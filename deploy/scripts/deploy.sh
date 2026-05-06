#!/usr/bin/env bash
set -euo pipefail

docker compose pull || true
docker compose build --no-cache bot
docker compose up -d postgres
docker compose run --rm bot alembic upgrade head
docker compose up -d bot
