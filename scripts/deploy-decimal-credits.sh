#!/bin/sh
set -eu
umask 077
project=/data1/rag-platform
release=${1:?release directory is required}
case "$release" in /data1/rag-releases/issue57-*) ;; *) exit 2 ;; esac
cd "$project"
test -s "$release/source.tar.gz"
docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.before.dump"
cp .env "$release/env.before"
cp docker-compose.yml "$release/compose.before.yml"
sha256sum .env docker-compose.yml > "$release/config.sha256"
tar --exclude=node_modules --exclude=.next --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ \
  -czf "$release/source.before.tar.gz" backend frontend scripts docs
for service in api web worker; do
  image=$(docker inspect "$(docker compose ps -q "$service")" --format '{{.Image}}')
  docker tag "$image" "rag-platform-$service:before-issue57"
  printf '%s %s\n' "$service" "$image" >> "$release/images.before"
done
docker compose ps -q db redis minio > "$release/data-containers.before"
tar -xzf "$release/source.tar.gz" -C "$project"
docker compose build api web worker > "$release/build.log" 2>&1
candidate=$(docker image inspect rag-platform-api --format '{{.Id}}')
docker run --rm --network none "$candidate" python -m pytest -q --tb=short > "$release/tests.log" 2>&1
paused=$(docker compose exec -T db psql -U rag -d rag -Atc \
  'SELECT pricing_paused FROM model_billing_settings WHERE id=1')
test "$paused" = f
docker compose exec -T db psql -U rag -d rag -c \
  'UPDATE model_billing_settings SET pricing_paused=true WHERE id=1' > /dev/null
resume() {
  docker compose exec -T db psql -U rag -d rag -c \
    'UPDATE model_billing_settings SET pricing_paused=false WHERE id=1' > /dev/null
}
trap resume EXIT
drained=0
for attempt in $(seq 1 60); do
  active=$(docker compose exec -T db psql -U rag -d rag -Atc \
    "SELECT (SELECT count(*) FROM conversations WHERE active_request_id IS NOT NULL)
      + (SELECT count(*) FROM model_usage_requests WHERE status='running')
      + (SELECT count(*) FROM conversation_context_operations WHERE status='running')
      + (SELECT count(*) FROM ingestion_jobs WHERE state='RUNNING');")
  if [ "$active" = 0 ]; then drained=1; break; fi
  sleep 2
done
test "$drained" = 1
docker compose stop -t 120 api web worker
docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.drained.dump"
docker compose run --rm --no-deps -v "$project/scripts:/verify:ro" --entrypoint python \
  api /verify/verify-decimal-billing.py snapshot > "$release/accounting.before.json"
docker compose run --rm --no-deps --entrypoint alembic api upgrade head > "$release/migration.log" 2>&1
docker compose run --rm --no-deps -v "$project/scripts:/verify:ro" -v "$release:/release:ro" \
  --entrypoint python api /verify/verify-decimal-billing.py check --file /release/accounting.before.json
resume
trap - EXIT
docker compose up -d --no-deps api web worker
ready=0
for attempt in $(seq 1 50); do
  if curl -fsS http://127.0.0.1:8000/health > "$release/health.json" &&
     curl -fsS http://127.0.0.1:3000/admin/pricing/template > /dev/null; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
sha256sum -c "$release/config.sha256"
docker compose ps -q db redis minio > "$release/data-containers.after"
cmp "$release/data-containers.before" "$release/data-containers.after"
test "$(docker compose exec -T db psql -U rag -d rag -Atc 'SELECT version_num FROM alembic_version')" = '0036_decimal_credits'
touch "$release/completed"
printf '%s\n' 'Decimal billing deployed; prices, multipliers, history and billing switches unchanged.'
