# Cloud Run image for the hosted AdLoop MCP server (Phase G).
#
# Runs the ASGI/HTTP entrypoint (adloop.asgi:main) in *server* mode. Cloud Run
# injects $PORT; main() binds 0.0.0.0:$PORT. Auth, per-user credentials, and the
# Supabase datastore are all env-gated and installed at startup (see asgi.py).
#
# Built in the cloud by `gcloud run deploy --source .` (Cloud Build detects this
# Dockerfile) — no local Docker required. To build locally instead:
#   docker build -t adloop-hosted .
#   docker run -p 8080:8080 --env-file .env adloop-hosted

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# uv: copy deps into the image (no venv symlinks), compile bytecode for faster
# cold starts, and never try to manage/download a Python (use the base image's).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer cached unless the lock/manifest changes).
# The `hosting` extra pulls in psycopg[binary,pool] for the Supabase datastore.
# README.md is required because pyproject's `readme = "README.md"` is read at build.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# No BuildKit cache mount: Cloud Build's `--source` builds use the classic
# docker builder, which rejects `RUN --mount=...`. A plain RUN is portable.
RUN uv sync --extra hosting --no-dev --frozen

ENV PATH="/app/.venv/bin:$PATH"

# v1 (Phase F): expose only the Ads + GA4 toolsets (core tools always survive).
# Overridable at deploy, but a comma-valued env is awkward via --set-env-vars,
# so it lives here as the baked default. Matches Phase C's ads+ga4-only creds.
ENV ADLOOP_TOOLSETS=ads,ga4,gtm,gsc

# Cloud Run's conventional default; main() honors $PORT if Cloud Run overrides it.
ENV PORT=8080
EXPOSE 8080

CMD ["python", "-m", "adloop.asgi"]
