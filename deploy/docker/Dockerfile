# syntax=docker/dockerfile:1
# Xplogent server image. Build from the repo root:
#   docker build -f deploy/docker/Dockerfile -t xplogent .
#
# Stage 1 builds the React dashboard, stage 2 installs the pinned Python
# dependencies (requirements/server.lock) and the app as a normal (non-editable)
# package, so this image also proves the wheel packaging works.

FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tini: correct signal handling / zombie reaping as PID 1.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements/server.lock requirements/server.lock
RUN pip install -r requirements/server.lock

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

COPY --from=web /web/dist /app/web_dist

# Unprivileged user; all state lives in the /data volume.
RUN useradd --system --uid 10001 --create-home --home-dir /home/xplogent xplogent \
    && mkdir -p /data && chown xplogent:xplogent /data
USER xplogent

ENV XPLOGENT_HOME=/data \
    XPLOGENT_WEB_DIR=/app/web_dist \
    XPLOGENT_API_HOST=0.0.0.0 \
    XPLOGENT_API_PORT=8765 \
    XPLOGENT_BEHIND_PROXY=1 \
    PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/health', timeout=4)"

ENTRYPOINT ["tini", "--"]
CMD ["xplogent", "up", "--no-browser", "--host", "0.0.0.0", "--port", "8765"]
