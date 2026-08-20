# Multi-stage: the build stage has uv, a compiler toolchain and the dev dependencies; the
# runtime stage has none of them. Anything present in the final image is attack surface that
# has to be patched, so the goal is a runtime containing Python, the virtualenv and nothing else.
#
# One image serves all three processes. The API, the worker and the migration job differ only
# by their command, so building three would mean three things to keep in step and three chances
# for the code and the schema to skew apart.

# --------------------------------------------------------------------- build

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

# Copy into the venv rather than hardlinking from the cache: the cache mount does not exist in
# the runtime stage, and a hardlink to a missing file is a broken venv.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, project second. Dependencies change rarely and application code changes
# every commit, so separate layers mean a code-only change reuses the cached dependency layer
# instead of resolving and downloading everything again.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# README.md is not documentation here: pyproject.toml declares `readme = "README.md"`, so the
# build backend reads it to produce the package metadata and fails without it.
COPY README.md ./
COPY src/ src/

# --no-editable installs a real copy into site-packages, templates and stylesheet included,
# instead of a link back to /app/src. That makes the runtime stage self-contained - it needs
# the venv and nothing else.
# --no-dev: pytest, ruff and mypy have no business in a production image.
# --frozen: fail if uv.lock disagrees with pyproject.toml rather than silently resolving
# something the test suite never ran against.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --------------------------------------------------------------------- runtime

FROM python:3.13-slim-bookworm AS runtime

# Unbuffered so logs reach the collector as they happen rather than when a buffer fills -
# without it a container that gets killed loses the lines explaining why.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# A fixed uid, not just a name: Kubernetes `runAsUser` needs a number, and a numeric uid also
# lets a volume be chowned to it without consulting the image.
RUN groupadd --gid 10001 hookline \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin hookline

WORKDIR /app

# curl only, for the container healthcheck. No build tools, nothing else.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build --chown=10001:10001 /app/.venv /app/.venv

# Migrations are not part of the installed package - they are operational scripts run by a
# separate job - so they are copied in explicitly.
COPY --chown=10001:10001 alembic.ini ./
COPY --chown=10001:10001 alembic/ alembic/

USER 10001:10001

EXPOSE 8000 9100

# Liveness only. It deliberately does not touch Postgres, so a database blip does not make
# Docker restart a perfectly healthy container. Readiness is /ready, which Kubernetes uses.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# The API by default. The worker overrides this with `hookline-worker`, and the migration job
# with `alembic upgrade head`.
CMD ["uvicorn", "hookline.main:app", "--host", "0.0.0.0", "--port", "8000"]
