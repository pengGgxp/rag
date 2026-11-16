FROM local/zhiku-cloud114-api:test
COPY backend/app /app/app
COPY backend/tests /app/tests
COPY backend/alembic /app/alembic
