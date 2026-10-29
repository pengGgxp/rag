#!/bin/sh
set -eu
umask 077
release=${1:?release directory required}
phase=${2:?prepare or switch required}
case "$release" in /data1/rag-releases/issue52-*) ;; *) exit 2 ;; esac
cd /data1/rag-platform
snapshot() {
  docker compose exec -T db psql -U rag -d rag -Atc \
    "SELECT id,connection_id,key,name,model,enabled,published,version,thinking_version,md5(thinking_config::text),context_capacity,max_output_tokens,md5(prices::text),template_version FROM site_model_providers ORDER BY id;
     SELECT id,name,platform,md5(base_url),md5(encrypted_api_key),timeout_seconds,enabled,version FROM model_connections ORDER BY id;
     SELECT id,enabled,shared_pricing_enabled,default_provider_id,md5(template::text),version FROM model_billing_settings ORDER BY id;
     SELECT id,billing_multiplier_percent,billing_version FROM workspaces ORDER BY id;
     SELECT workspace_id,balance,lifetime_granted,lifetime_spent FROM credit_accounts ORDER BY workspace_id;
     SELECT id,site_provider_id,workspace_id,active FROM llm_provider_configs ORDER BY id;"
}
if [ "$phase" = prepare ]; then
  test -s "$release/source.tar.gz"
  test ! -e "$release/prepared"
  docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.before-build.dump"
  cp .env "$release/env.before"
  cp docker-compose.yml "$release/compose.before.yml"
  sha256sum .env docker-compose.yml > "$release/config.sha256"
  tar --exclude=node_modules --exclude=.next --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ \
    -czf "$release/source.before.tar.gz" backend frontend
  for service in api web; do
    image=$(docker inspect "$(docker compose ps -q "$service")" --format '{{.Image}}')
    docker tag "$image" "rag-platform-$service:before-issue52"
    printf '%s %s\n' "$service" "$image" >> "$release/images.before"
  done
  docker compose ps -q db redis minio worker > "$release/unchanged.before"
  tar -xzf "$release/source.tar.gz" -C /data1/rag-platform backend frontend scripts docs
  docker compose build api web > "$release/build.log" 2>&1
  candidate=$(docker image inspect rag-platform-api --format '{{.Id}}')
  docker run --rm --network none "$candidate" python -m pytest -q > "$release/tests.log" 2>&1
  sha256sum -c "$release/config.sha256"
  touch "$release/prepared"
  exit 0
fi
test "$phase" = switch
test -e "$release/prepared"
test ! -e "$release/completed"
original_pause=$(docker compose exec -T db psql -U rag -d rag -Atc \
  'SELECT pricing_paused FROM model_billing_settings WHERE id=1')
restore_pause() {
  docker compose exec -T db psql -U rag -d rag -c \
    "UPDATE model_billing_settings SET pricing_paused='$original_pause' WHERE id=1" > /dev/null
}
trap restore_pause EXIT
docker compose exec -T db psql -U rag -d rag -c \
  'UPDATE model_billing_settings SET pricing_paused=true WHERE id=1' > /dev/null
drained=0
for attempt in $(seq 1 30); do
  active=$(docker compose exec -T db psql -U rag -d rag -Atc \
    "SELECT (SELECT count(*) FROM conversations WHERE active_request_id IS NOT NULL)
       + (SELECT count(*) FROM model_thinking_tests WHERE status='running')
       + (SELECT count(*) FROM conversation_context_operations WHERE status='running')")
  if [ "$active" = 0 ]; then drained=1; break; fi
  sleep 2
done
test "$drained" = 1
docker compose stop api
docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.dump"
snapshot > "$release/data.before"
docker compose run --rm --no-deps api alembic upgrade head > "$release/migration.log" 2>&1
snapshot > "$release/data.after"
cmp "$release/data.before" "$release/data.after"
test "$(docker compose exec -T db psql -U rag -d rag -Atc \
  "SELECT count(*) FROM model_connections WHERE api_protocol <> 'chat_completions'")" = 0
test "$(docker compose exec -T db psql -U rag -d rag -Atc \
  'SELECT version_num FROM alembic_version')" = '0034_model_api_protocol'
restore_pause
docker compose up -d --no-deps api web
ready=0
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health > "$release/health.json" &&
     curl -fsS http://127.0.0.1:3000/admin/models/new > /dev/null; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
sha256sum -c "$release/config.sha256"
docker compose ps -q db redis minio worker > "$release/unchanged.after"
cmp "$release/unchanged.before" "$release/unchanged.after"
touch "$release/completed"
printf '%s\n' 'Explicit API protocols deployed; existing calls remain Chat Completions.'
