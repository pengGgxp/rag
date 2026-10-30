#!/bin/sh
set -eu
umask 077
project=/data1/rag-platform
release=/data1/rag-releases/cache-billing-20261030
cd "$project"
test -s "$release/source.tar.gz"
docker compose exec -T db pg_dump -U rag -d rag -Fc > "$release/database.before.dump"
cp .env "$release/env.before"
cp docker-compose.yml "$release/compose.before.yml"
sha256sum .env docker-compose.yml > "$release/config.sha256"
tar -czf "$release/source.before.tar.gz" backend/app/model_pricing.py backend/tests/test_model_pricing.py \
  backend/tests/test_decimal_credits.py docs/model-cache-billing.md
image=$(docker inspect "$(docker compose ps -q api)" --format '{{.Image}}')
docker tag "$image" rag-platform-api:before-cache-billing
docker compose ps -q db redis minio worker web > "$release/containers.before"
tar -xzf "$release/source.tar.gz" -C "$project"
docker compose build api > "$release/build.log" 2>&1
docker run --rm --network none rag-platform-api python -m pytest -q --tb=short > "$release/tests.log" 2>&1

testdb=rag_auth_test_cache_20261030
docker compose exec -T db createdb -U rag "$testdb"
cleanup_test() { docker compose exec -T db dropdb -U rag "$testdb"; }
trap cleanup_test EXIT
testurl="postgresql+psycopg://rag:rag@db:5432/$testdb"
docker compose run --rm --no-deps -e DATABASE_URL="$testurl" --entrypoint alembic api upgrade head \
  > "$release/postgres-migration.log" 2>&1
docker compose run --rm --no-deps -e AUTH_TEST_DATABASE_URL="$testurl" --entrypoint python api -m pytest -q \
  tests/test_model_billing_postgres.py tests/test_model_pricing_postgres.py \
  tests/test_decimal_credits_postgres.py tests/test_workspace_billing_postgres.py \
  > "$release/postgres-tests.log" 2>&1
cleanup_test
trap - EXIT
touch "$release/verified"
printf '%s\n' 'Build and regression checks passed; ready for drained activation.'
