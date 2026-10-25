# syntax=docker/dockerfile:1
# Run 5, Phase 45: one Dockerfile, three stages.
#   webbuild - builds the UI in API mode (VITE_DATA_SOURCE=http)
#   api      - the engine + BFF (uvicorn); the compose file also uses this
#              stage as a one-shot job that exports the demo bundle
#   ui       - nginx serving the built UI, /api proxied to the api service
# The refresh scheduler runs INSIDE the api process (api/refresh.py starts in
# the app lifespan), so compose needs no separate scheduler container.

FROM node:22-bookworm-slim AS webbuild
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
ENV VITE_DATA_SOURCE=http
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS api
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Dependency layer first (cached unless the lock changes): runtime + api
# groups only, never dev tooling.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --group api
# Project code. synth/ ships so the demo-world export works in-container.
COPY src/ src/
COPY api/ api/
COPY config/ config/
COPY synth/ synth/
COPY data/sample_online_retail/ data/sample_online_retail/
RUN uv sync --frozen --no-dev --group api
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM nginx:1.27-alpine AS ui
COPY --from=webbuild /app/web/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
