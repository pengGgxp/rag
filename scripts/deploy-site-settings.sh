#!/bin/sh
set -eu
umask 077
project=/data1/rag-platform
release=${1:?release directory is required}
case "$release" in /data1/rag-releases/issue48-*) ;; *) exit 2 ;; esac
cd "$project"
test -s "$release/source.tar.gz"
snapshot() {
  docker compose exec -T db psql -U rag -d rag -Atc \
    "SELECT id, md5(prices::text), version FROM site_model_providers ORDER BY id;
     SELECT id, enabled, shared_pricing_enabled, default_provider_id, md5(template::text), version FROM model_billing_settings ORDER BY id;
     SELECT id, billing_multiplier_percent, billing_version FROM workspaces ORDER BY id;
     SELECT workspace_id, balance, lifetime_granted, lifetime_spent FROM credit_accounts ORDER BY workspace_id;
     SELECT id, active, version FROM site_storage_settings ORDER BY id;"
}
docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.dump"
cp .env "$release/env.before"
cp docker-compose.yml "$release/compose.before.yml"
sha256sum .env docker-compose.yml > "$release/config.sha256"
snapshot > "$release/data.before"
tar --exclude=node_modules --exclude=.next --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ -czf "$release/source.before.tar.gz" backend frontend
for service in api web; do
  image=$(docker inspect "$(docker compose ps -q "$service")" --format '{{.Image}}')
  docker tag "$image" "rag-platform-$service:before-issue48"
  printf '%s %s\n' "$service" "$image" >> "$release/images.before"
done
docker compose ps -q db redis minio worker > "$release/unchanged.before"
tar -xzf "$release/source.tar.gz" -C "$project"
docker compose build api web
candidate=$(docker image inspect rag-platform-api --format '{{.Id}}')
docker run --rm --network none "$candidate" python -m pytest -q \
  tests/test_site_settings.py tests/test_auth.py tests/test_credits.py tests/test_email_auth.py \
  tests/test_site_admin.py tests/test_payments.py tests/test_model_pricing.py \
  tests/test_workspace_billing.py tests/test_pricing_calculator.py > "$release/tests.log" 2>&1
active=$(docker compose exec -T db psql -U rag -d rag -Atc 'SELECT count(*) FROM conversations WHERE active_request_id IS NOT NULL')
test "$active" = 0
docker compose up -d --no-deps api web
ready=0
for attempt in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:8000/health > "$release/health.json" &&
     curl -fsS http://127.0.0.1:3000/admin/settings > /dev/null; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
sha256sum -c "$release/config.sha256"
snapshot > "$release/data.after"
cmp "$release/data.before" "$release/data.after"
docker compose ps -q db redis minio worker > "$release/unchanged.after"
cmp "$release/unchanged.before" "$release/unchanged.after"
docker compose exec -T db psql -U rag -d rag -Atc \
  "SELECT version, signup_credits, announcement_published FROM site_settings WHERE id=1;" > "$release/settings.after"
test "$(cat "$release/settings.after")" = '0|1000|f'
touch "$release/completed"
printf '%s\n' 'Site settings deployed; existing balances and pricing unchanged.'
