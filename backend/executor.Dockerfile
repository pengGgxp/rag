FROM python:3.12-alpine
RUN apk add --no-cache docker-cli nodejs bash ca-certificates
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn[standard]==0.34.0 pydantic==2.10.4
COPY app ./app
