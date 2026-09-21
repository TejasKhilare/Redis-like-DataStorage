# syntax=docker/dockerfile:1

# ---- build: produce a wheel so the runtime image carries no build tooling
FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

# ---- runtime
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KV_HOST=0.0.0.0 \
    KV_DATA_DIR=/data

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl \
    && useradd --create-home --uid 10001 kv \
    && mkdir /data && chown kv:kv /data

USER kv
VOLUME ["/data"]
EXPOSE 8000 6379

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"KV_HTTP_PORT\", \"8000\")}/health', timeout=2)"

CMD ["python", "-m", "kvstore"]
