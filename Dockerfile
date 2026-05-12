# ── Multi-stage Docker build ───────────────────────────
# Stage 1: slim Python with deps only
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install only requirements (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: production image
FROM python:3.12-slim

RUN groupadd -r memory && useradd -r -g memory memory

ENV MEMORY_DATA_DIR=/data \
    MEMORY_HOST=0.0.0.0 \
    MEMORY_PORT=8650 \
    MEMORY_LOG_LEVEL=INFO \
    MEMORY_API_KEY=""

WORKDIR /app

COPY --from=base /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=base /usr/local/bin /usr/local/bin
COPY server.py .

RUN mkdir -p /data && chown -R memory:memory /data /app

USER memory

EXPOSE 8650

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8650/health')" || exit 1

CMD ["python3", "server.py"]
