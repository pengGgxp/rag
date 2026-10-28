#!/bin/sh
set -eu
umask 077
project=/data1/rag-platform
release=${1:?release directory is required}
case "$release" in /data1/rag-releases/issue45-*) ;; *) exit 2 ;; esac
cd "$project"
test -s "$release/source.tar.gz"
if [ ! -f "$release/images.before" ]; then
  docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.dump"
  cp .env "$release/env.before"
  cp docker-compose.yml "$release/compose.before.yml"
  sha256sum .env > "$release/env.sha256"
  docker compose exec -T db psql -U rag -d rag -Atc \
    "SELECT id, md5(prices::text) FROM site_model_providers ORDER BY id; SELECT id, enabled, shared_pricing_enabled, default_provider_id, md5(template::text) FROM model_billing_settings ORDER BY id; SELECT id, billing_multiplier_percent FROM workspaces ORDER BY id" > "$release/billing.before"
  tar --exclude=node_modules --exclude=.next --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ -czf "$release/source.before.tar.gz" backend frontend
  api_image=$(docker inspect "$(docker compose ps -q api)" --format '{{.Image}}')
  web_image=$(docker inspect "$(docker compose ps -q web)" --format '{{.Image}}')
  docker tag "$api_image" rag-platform-api:before-issue45
  docker tag "$web_image" rag-platform-web:before-issue45
  docker compose ps -q db redis minio worker > "$release/data-containers.before"
  printf '%s\n%s\n' "$api_image" "$web_image" > "$release/images.before"
fi
tar -xzf "$release/source.tar.gz" -C "$project"
docker compose build api web
active=$(docker compose exec -T db psql -U rag -d rag -Atc 'SELECT count(*) FROM conversations WHERE active_request_id IS NOT NULL')
test "$active" = 0
docker compose stop api
docker compose run --rm --no-deps api alembic upgrade head
docker compose up -d --no-deps api web
ready=0
for attempt in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:8000/health > "$release/health.json" && curl -fsS http://127.0.0.1:3000/ > /dev/null; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
sha256sum -c "$release/env.sha256"
docker compose exec -T db psql -U rag -d rag -Atc \
  "SELECT id, md5(prices::text) FROM site_model_providers ORDER BY id; SELECT id, enabled, shared_pricing_enabled, default_provider_id, md5(template::text) FROM model_billing_settings ORDER BY id; SELECT id, billing_multiplier_percent FROM workspaces ORDER BY id" > "$release/billing.after"
cmp "$release/billing.before" "$release/billing.after"
docker compose ps -q db redis minio worker > "$release/data-containers.after"
cmp "$release/data-containers.before" "$release/data-containers.after"
docker compose exec -T api alembic current
test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/dev/conversation)" = 404
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
touch "$release/completed"
