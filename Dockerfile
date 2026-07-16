FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ARG UV_VERSION=0.11.29

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN python -m pip install --index-url "${PIP_INDEX_URL}" --upgrade pip \
    && python -m pip install --index-url "${PIP_INDEX_URL}" "uv==${UV_VERSION}" \
    && uv export --frozen --no-dev --extra providers --no-emit-project --output-file /tmp/requirements.lock \
    && uv pip install --system --index-url "${PIP_INDEX_URL}" --require-hashes -r /tmp/requirements.lock \
    && rm -f /tmp/requirements.lock \
    && groupadd --gid 10001 quant \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin quant

COPY --chown=10001:10001 src ./src
COPY --chown=10001:10001 apps/__init__.py ./apps/__init__.py
COPY --chown=10001:10001 apps/api ./apps/api
COPY --chown=10001:10001 deploy/akshare-universe.csv ./config/akshare-universe.csv

ENV PYTHONPATH=/app/src:/app

RUN mkdir -p /app/data/research /app/backups \
    && chown -R 10001:10001 /app/data /app/backups

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
