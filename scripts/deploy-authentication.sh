#!/bin/sh
# Run on the authorized trial server after uploading the reviewed source archive.
set -eu
umask 077
project=/data1/rag-platform
release=/data1/rag-releases/authentication-20261026
archive=$release/source.tar.gz
mkdir -p /data1/rag-releases
mkdir -p "$release"
cd "$project"
test -f "$archive"
if [ ! -f "$release/database.dump" ]; then
  docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.dump"
  cp .env "$release/env.before"
  tar --exclude=frontend/node_modules --exclude=frontend/.next --exclude=backend/.venv --exclude=backend/.pytest_cache --exclude='*/__pycache__' -czf "$release/source.before.tar.gz" backend frontend docker-compose.yml
  api_image=$(docker inspect "$(docker compose ps -q api)" --format '{{.Image}}')
  web_image=$(docker inspect "$(docker compose ps -q web)" --format '{{.Image}}')
  docker inspect "$(docker compose ps -q api)" "$(docker compose ps -q web)" --format '{{.Name}} {{.Image}}' > "$release/images.before"
  docker tag "$api_image" rag-platform-api:before-auth-20261026
  docker tag "$web_image" rag-platform-web:before-auth-20261026
  cp /www/server/panel/vhost/nginx/zk.str1ct.top.conf "$release/nginx.before.conf"
fi
tar -xzf "$archive" -C "$project"
if ! grep -q '^SITE_CONFIG_ENCRYPTION_KEY=.' .env; then
  site_key=$(docker compose exec -T api python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
  printf '\n# Persistent site SMTP / WeChat encryption key; keep backed up.\nSITE_CONFIG_ENCRYPTION_KEY=%s\n' "$site_key" >> .env
  unset site_key
fi
if [ -f "$release/zk.str1ct.top.conf.new" ]; then
  cp "$release/zk.str1ct.top.conf.new" /www/server/panel/vhost/nginx/zk.str1ct.top.conf
  nginx -t
  nginx -s reload
fi
cp .env "$release/env.with-site-key"
chmod 600 .env "$release"/env.*
docker compose build api web
docker compose up -d --no-deps api web
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health > "$release/health.json"; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:8000/health
docker compose exec -T api python -m app.manage site-admin grant --email str1ct@qq.com
docker compose ps
