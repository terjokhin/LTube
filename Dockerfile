# Stage 1: build the Svelte UI into src/ltube/web/static
FROM node:24-slim AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json web/.npmrc ./
RUN npm ci
COPY web/ ./
# vite.config.ts writes to ../src/ltube/web/static, i.e. /build/src/ltube/web/static
RUN npm run build

# Stage 2: Python app; ffmpeg and Deno come from Python wheels, no apt packages needed
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY --from=web /build/src/ltube/web/static ./src/ltube/web/static
RUN uv sync --frozen --no-dev

# Run unprivileged; files land in the mounted music folder owned by this uid.
# Override with `user:` in compose if your host user isn't uid 1000 (not needed on Docker Desktop for Mac).
RUN useradd --uid 1000 --create-home ltube && mkdir -p /music /data && chown ltube:ltube /music /data
USER ltube

# The download folder is whatever host folder is mounted at /music, so the UI can't change it.
ENV PATH=/app/.venv/bin:$PATH \
    LTUBE_HOST=0.0.0.0 \
    LTUBE_PORT=8000 \
    LTUBE_OUT=/music \
    LTUBE_LOCK_OUT=1 \
    LTUBE_DATA=/data \
    XDG_CACHE_HOME=/data/cache
VOLUME ["/music", "/data"]
EXPOSE 8000
CMD ["ltube", "serve"]
