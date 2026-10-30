#!/bin/sh
set -eu
umask 077
project=/data1/rag-platform
release=/data1/rag-releases/cache-billing-20261030
cd "$project"
test -f "$release/verified"
docker compose exec -T db psql -U rag -d rag -v ON_ERROR_STOP=1 -c \
  'UPDATE model_billing_settings SET pricing_paused=true WHERE id=1' > /dev/null
restore_service() {
  docker compose exec -T db psql -U rag -d rag -c \
    'UPDATE model_billing_settings SET pricing_paused=false WHERE id=1' > /dev/null
  docker compose up -d --no-deps api
}
trap restore_service EXIT
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
docker compose stop -t 60 api
docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.drained.dump"
docker compose run --rm --no-deps -e PYTHONPATH=/app -v "$project/scripts:/verify:ro" \
  --entrypoint python api /verify/activate-cache-billing.py > "$release/activation.json"
restore_service
trap - EXIT
sha256sum -c "$release/config.sha256"
docker compose ps -q db redis minio worker web > "$release/containers.after"
cmp "$release/containers.before" "$release/containers.after"
touch "$release/activated"
printf '%s\n' 'Cache settlement enabled; confirmed output units corrected; historical accounting preserved.'
