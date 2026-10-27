#!/bin/sh
# Preserve existing configuration and data services; update only API and Web.
set -eu
umask 077
project=/data1/rag-platform
release=${1:?release directory is required}
case "$release" in /data1/rag-releases/settings-*) ;; *) exit 2 ;; esac
cd "$project"
test -s "$release/source.tar.gz"
if [ ! -f "$release/images.before" ]; then
  docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.dump"
  cp .env "$release/env.before"
  cp docker-compose.yml "$release/compose.before.yml"
  sha256sum .env > "$release/env.sha256"
  docker compose exec -T db psql -U rag -d rag -Atc 'SELECT md5(config::text) FROM site_auth_settings ORDER BY id' > "$release/auth-config.before"
  tar --exclude=node_modules --exclude=.next --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ -czf "$release/source.before.tar.gz" backend frontend
  api_image=$(docker inspect "$(docker compose ps -q api)" --format '{{.Image}}')
  web_image=$(docker inspect "$(docker compose ps -q web)" --format '{{.Image}}')
  docker tag "$api_image" rag-platform-api:before-settings-issue37
  docker tag "$web_image" rag-platform-web:before-settings-issue37
  docker compose ps -q db redis minio worker > "$release/data-containers.before"
  printf '%s\n%s\n' "$api_image" "$web_image" > "$release/images.before"
fi
tar -xzf "$release/source.tar.gz" -C "$project"
docker compose build api web
docker compose up -d --no-deps api web
ready=0
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health > "$release/health.json" && curl -fsS http://127.0.0.1:3000/admin/models > /dev/null; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
sha256sum -c "$release/env.sha256"
docker compose exec -T db psql -U rag -d rag -Atc 'SELECT md5(config::text) FROM site_auth_settings ORDER BY id' > "$release/auth-config.after"
cmp "$release/auth-config.before" "$release/auth-config.after"
docker compose ps -q db redis minio worker > "$release/data-containers.after"
cmp "$release/data-containers.before" "$release/data-containers.after"
docker compose exec -T api alembic current
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
