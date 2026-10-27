#!/bin/sh
# Stage configuration and durable metering without enabling a new billing formula.
set -eu
umask 077
project=/data1/rag-platform
release=${1:?release directory is required}
case "$release" in /data1/rag-releases/model-billing-*) ;; *) exit 2 ;; esac
cd "$project"
test -s "$release/source.tar.gz"
prices='COPY (SELECT id, workspace_id, provider_id, capability, unit, unit_size, credits_per_unit, enabled, version FROM usage_policies ORDER BY id) TO STDOUT'
if [ ! -f "$release/images.before" ]; then
  docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.dump"
  cp .env "$release/env.before"
  cp docker-compose.yml "$release/compose.before.yml"
  sha256sum .env > "$release/env.sha256"
  docker compose exec -T db psql -U rag -d rag -Atc 'SELECT md5(config::text) FROM site_auth_settings ORDER BY id' > "$release/auth-config.before"
  docker compose exec -T db psql -U rag -d rag -Atc "$prices" > "$release/prices.before"
  tar --exclude=node_modules --exclude=.next --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ -czf "$release/source.before.tar.gz" backend frontend
  api_image=$(docker inspect "$(docker compose ps -q api)" --format '{{.Image}}')
  web_image=$(docker inspect "$(docker compose ps -q web)" --format '{{.Image}}')
  docker tag "$api_image" rag-platform-api:before-model-billing-issue36
  docker tag "$web_image" rag-platform-web:before-model-billing-issue36
  docker compose ps -q db redis minio worker > "$release/data-containers.before"
  printf '%s\n%s\n' "$api_image" "$web_image" > "$release/images.before"
fi
tar -xzf "$release/source.tar.gz" -C "$project"
docker compose build api web
active=$(docker compose exec -T db psql -U rag -d rag -Atc 'SELECT count(*) FROM conversations WHERE active_request_id IS NOT NULL')
if [ "$active" != 0 ]; then
  printf 'Active conversations exist; drain them and rerun deployment.\n' >&2
  exit 3
fi
docker compose up -d --no-deps api web
ready=0
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health > "$release/health.json" && curl -fsS http://127.0.0.1:3000/admin/pricing/template > /dev/null; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
sha256sum -c "$release/env.sha256"
docker compose exec -T db psql -U rag -d rag -Atc 'SELECT md5(config::text) FROM site_auth_settings ORDER BY id' > "$release/auth-config.after"
cmp "$release/auth-config.before" "$release/auth-config.after"
docker compose exec -T db psql -U rag -d rag -Atc "$prices" > "$release/prices.after"
cmp "$release/prices.before" "$release/prices.after"
docker compose ps -q db redis minio worker > "$release/data-containers.after"
cmp "$release/data-containers.before" "$release/data-containers.after"
test "$(docker compose exec -T db psql -U rag -d rag -Atc 'SELECT enabled FROM model_billing_settings WHERE id=1')" = f
docker compose exec -T api python -m app.manage model-billing status
docker compose exec -T api alembic current
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
touch "$release/completed"
