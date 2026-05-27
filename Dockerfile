# ── Memory Gateway v4 Docker ──────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEMORY_DATA_DIR=/data \
    MEMORY_HOST=0.0.0.0 \
    MEMORY_PORT=8650 \
    MEMORY_LOG_LEVEL=INFO

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py entrypoint.sh ./
COPY static/ ./static/
RUN chmod +x entrypoint.sh && mkdir -p /data

EXPOSE 8650

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8650/health')" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
