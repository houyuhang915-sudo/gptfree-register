FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    FREE_CONSOLE_TIMEZONE=Asia/Shanghai \
    FREE_CONSOLE_HOST=0.0.0.0 \
    FREE_CONSOLE_PORT=8866

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl nodejs npm chromium fonts-noto-cjk tzdata \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN cd /app/core && npm ci --omit=dev

RUN groupadd --system --gid 10001 freeops \
    && useradd --system --uid 10001 --gid freeops --home-dir /app freeops \
    && mkdir -p /app/data /app/output \
    && chown -R freeops:freeops /app

USER freeops
EXPOSE 8866
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${FREE_CONSOLE_PORT:-8866}/api/health >/dev/null || exit 1

# A single Gunicorn process owns the in-memory subprocess handles. Threads keep
# polling and job-control requests concurrent.
CMD ["sh", "-c", "gunicorn --workers 1 --threads ${FREE_CONSOLE_THREADS:-8} --timeout 120 --bind 0.0.0.0:${FREE_CONSOLE_PORT:-8866} app:app"]
