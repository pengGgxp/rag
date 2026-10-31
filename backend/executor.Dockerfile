FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends docker.io && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn[standard]==0.34.0 pydantic==2.10.4
COPY app ./app
