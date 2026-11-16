# Reuse the deployed dependency set; application, migrations and tests are current.
FROM swr.cn-south-1.myhuaweicloud.com/str1ct/zhiku-api@sha256:2028e468db7065ccc3eae418e41020077a58a847626e176646f48bf6e4c3c83e
COPY backend/app /app/app
COPY backend/tests /app/tests
COPY backend/alembic /app/alembic
