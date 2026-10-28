#!/bin/sh
set -eu
umask 077
project=/data1/rag-platform
release=${1:?release directory is required}
case "$release" in /data1/rag-releases/issue46-*) ;; *) exit 2 ;; esac
cd "$project"
test -s "$release/source.tar.gz"
if [ ! -f "$release/images.before" ]; then
  docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.dump"
  cp .env "$release/env.before"
  cp docker-compose.yml "$release/compose.before.yml"
  sha256sum .env > "$release/env.sha256"
  docker compose exec -T db psql -U rag -d rag -Atc \
    "SELECT id, object_key, size_bytes, content_hash FROM documents ORDER BY id; SELECT id, md5(prices::text) FROM site_model_providers ORDER BY id; SELECT id, enabled, shared_pricing_enabled, default_provider_id, md5(template::text) FROM model_billing_settings ORDER BY id; SELECT id, billing_multiplier_percent FROM workspaces ORDER BY id; SELECT workspace_id, balance, lifetime_spent FROM credit_accounts ORDER BY workspace_id" > "$release/data.before"
  tar --exclude=node_modules --exclude=.next --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ -czf "$release/source.before.tar.gz" backend frontend
  for service in api worker web; do
    image=$(docker inspect "$(docker compose ps -q "$service")" --format '{{.Image}}')
    docker tag "$image" "rag-platform-$service:before-issue46"
    printf '%s %s\n' "$service" "$image" >> "$release/images.before"
  done
  docker compose ps -q db redis minio > "$release/data-containers.before"
fi
tar -xzf "$release/source.tar.gz" -C "$project"
# Preserve the deployed Compose options; forward the existing encryption key only.
cp "$release/compose.before.yml" "$project/docker-compose.yml"
python3 - <<'PY'
import yaml
from pathlib import Path
path = Path("docker-compose.yml")
config = yaml.safe_load(path.read_text())
config["services"]["worker"].setdefault("environment", {})["SITE_CONFIG_ENCRYPTION_KEY"] = "${SITE_CONFIG_ENCRYPTION_KEY:-}"
path.write_text(yaml.safe_dump(config, sort_keys=False))
PY
docker compose config --quiet
docker compose build api worker web
# Tests have no network or deployed environment, and cannot reach paid services.
image_name=$(docker compose config --format json | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c["services"]["api"].get("image") or c["name"]+"-api")')
candidate=$(docker image inspect "$image_name" --format '{{.Id}}')
test -n "$candidate"
docker run --rm --network none "$candidate" python -m pytest -q > "$release/tests.log" 2>&1
running=$(docker compose exec -T db psql -U rag -d rag -Atc "SELECT count(*) FROM ingestion_jobs WHERE state = 'RUNNING'")
active=$(docker compose exec -T db psql -U rag -d rag -Atc "SELECT count(*) FROM conversations WHERE active_request_id IS NOT NULL")
test "$running" = 0
test "$active" = 0
docker compose stop api worker
docker compose run --rm --no-deps api alembic upgrade head
docker compose up -d --no-deps api worker web
ready=0
for attempt in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:8000/health > "$release/health.json" && curl -fsS http://127.0.0.1:3000/admin/storage > /dev/null; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
sha256sum -c "$release/env.sha256"
docker compose exec -T db psql -U rag -d rag -Atc \
  "SELECT id, object_key, size_bytes, content_hash FROM documents ORDER BY id; SELECT id, md5(prices::text) FROM site_model_providers ORDER BY id; SELECT id, enabled, shared_pricing_enabled, default_provider_id, md5(template::text) FROM model_billing_settings ORDER BY id; SELECT id, billing_multiplier_percent FROM workspaces ORDER BY id; SELECT workspace_id, balance, lifetime_spent FROM credit_accounts ORDER BY workspace_id" > "$release/data.after"
cmp "$release/data.before" "$release/data.after"
docker compose ps -q db redis minio > "$release/data-containers.after"
cmp "$release/data-containers.before" "$release/data-containers.after"
docker compose exec -T api alembic current
docker compose exec -T worker python -c "from app.config import get_settings; assert get_settings().site_config_encryption_key; print('worker encryption key available')"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
touch "$release/configuration-ready"
printf '%s\n' 'Application deployed. COS credentials, approved CORS, verified migration and real acceptance are still required.'
