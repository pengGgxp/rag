#!/bin/sh
# Authorized trial deployment: preserve data services, environment and saved credentials.
set -eu
umask 077
project=/data1/rag-platform
release=/data1/rag-releases/site-admin-20261026
cd "$project"
test -s "$release/source.tar.gz"
if [ ! -f "$release/database.dump" ]; then
  docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.dump"
  cp .env "$release/env.before"
  sha256sum .env > "$release/env.sha256"
  docker compose exec -T db psql -U rag -d rag -Atc 'SELECT md5(config::text) FROM site_auth_settings ORDER BY id' > "$release/auth-config.before"
  tar --exclude=frontend/node_modules --exclude=frontend/.next --exclude=backend/.venv --exclude=backend/.pytest_cache --exclude='*/__pycache__' -czf "$release/source.before.tar.gz" backend frontend
  api_image=$(docker inspect "$(docker compose ps -q api)" --format '{{.Image}}')
  web_image=$(docker inspect "$(docker compose ps -q web)" --format '{{.Image}}')
  docker tag "$api_image" rag-platform-api:before-site-admin-20261026
  docker tag "$web_image" rag-platform-web:before-site-admin-20261026
  printf '%s\n%s\n' "$api_image" "$web_image" > "$release/images.before"
fi
tar -xzf "$release/source.tar.gz" -C "$project"
docker compose build api web
docker compose up -d --no-deps api web
ready=0
for attempt in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health > "$release/health.json" && curl -fsS http://127.0.0.1:3000/admin > /dev/null; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
sha256sum -c "$release/env.sha256"
docker compose exec -T db psql -U rag -d rag -Atc 'SELECT md5(config::text) FROM site_auth_settings ORDER BY id' > "$release/auth-config.after"
cmp "$release/auth-config.before" "$release/auth-config.after"
docker compose exec -T api alembic current
docker compose ps
