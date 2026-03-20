# e-Consul slot monitor — web UI + background worker in one process (gunicorn threads).
# Run on EKS: mount or inject .env keys via Secret; expose port 8080.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# curl_cffi wheels are glibc-based; slim-bookworm is sufficient.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py web_app.py app.py ./
COPY examples ./examples
COPY .env.example ./.env.example
COPY .env ./.env

USER app

EXPOSE 8080

# Login may redirect when WEB_* auth is off — any 2xx/3xx counts as up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=4)" || exit 1

# Gunicorn: single master, multi-thread worker (Monitor daemon + Flask share process memory).
CMD exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --threads 4 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  web_app:app
