#!/bin/sh
# Preserve infrastructure, environment and saved site credentials; update ingestion services.
set -eu
umask 077
project=/data1/rag-platform
release=/data1/rag-releases/document-upload-20261026
nginx_config=/www/server/panel/vhost/nginx/zk.str1ct.top.conf
cd "$project"
test -s "$release/source.tar.gz"
if [ ! -f "$release/backups.complete" ]; then
  docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.dump.tmp"
  mv "$release/database.dump.tmp" "$release/database.dump"
  cp .env "$release/env.before"
  sha256sum .env > "$release/env.sha256"
  cp "$nginx_config" "$release/nginx.before.conf"
  tar --exclude=frontend/node_modules --exclude=frontend/.next --exclude=backend/.venv --exclude=backend/.pytest_cache --exclude='*/__pycache__' -czf "$release/source.before.tar.gz" backend frontend
  for service in api worker web; do
    image=$(docker inspect "$(docker compose ps -q "$service")" --format '{{.Image}}')
    docker tag "$image" "rag-platform-$service:before-document-upload-20261026"
    printf '%s %s\n' "$service" "$image" >> "$release/images.before"
  done
  docker compose exec -T db psql -U rag -d rag -Atc 'SELECT md5(config::text) FROM site_auth_settings ORDER BY id' > "$release/auth-config.before"
  touch "$release/backups.complete"
fi
tar -xzf "$release/source.tar.gz" -C "$project"
docker compose build api worker web
docker compose up -d --no-deps api worker web
ready=0
for attempt in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health > "$release/health.json" && curl -fsS http://127.0.0.1:3000/ > /dev/null; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
# Change only the inspected vhost directives, preserving its certificates and routes.
if ! grep -q 'client_max_body_size 201m;' "$nginx_config"; then
  sed -i 's/client_max_body_size 100m;/client_max_body_size 201m;\n    client_body_timeout 600s;/g; /proxy_read_timeout 3600s;/a\        proxy_send_timeout 600s;' "$nginx_config"
fi
if nginx -t; then
  nginx -s reload
else
  cp "$release/nginx.before.conf" "$nginx_config"
  exit 1
fi
sha256sum -c "$release/env.sha256"
docker compose exec -T db psql -U rag -d rag -Atc 'SELECT md5(config::text) FROM site_auth_settings ORDER BY id' > "$release/auth-config.after"
cmp "$release/auth-config.before" "$release/auth-config.after"
docker compose exec -T worker celery -A app.tasks.celery_app inspect ping --timeout 10
docker compose ps
