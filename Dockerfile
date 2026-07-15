FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY apps/__init__.py ./apps/__init__.py
COPY apps/api ./apps/api
COPY deploy/akshare-universe.csv ./config/akshare-universe.csv

RUN python -m pip install --index-url "${PIP_INDEX_URL}" --upgrade pip \
    && python -m pip install --index-url "${PIP_INDEX_URL}" ".[providers]"

RUN mkdir -p /app/data/research

EXPOSE 8000

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
