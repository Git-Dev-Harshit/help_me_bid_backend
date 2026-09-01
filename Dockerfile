# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Multi-stage build with two publishable targets.
#
#   builder    compiles dependencies into a self-contained virtualenv, so no
#              build toolchain ever reaches a shipped image
#   app-base   the actual application image: venv + code, no compilers
#   runtime    production target (DEFAULT - it is the last stage, so a bare
#              `docker build .` produces the production image, never the test one)
#   test       runtime + pytest/ruff/mypy + the test suite
#
# `test` derives FROM app-base, so the test suite runs against exactly the
# Python, dependency set and application code that ship to production - while
# the production image itself carries no test tooling.
#
# Build explicitly with:
#     docker build --target runtime -t ipo-tracker:latest .
#     docker build --target test    -t ipo-tracker-test:latest .
# Both compose files pass `target:` for this reason.
#
# The same image runs the API, the worker and the migration step - only the
# command differs - so all three are guaranteed to share one dependency set.
# ---------------------------------------------------------------------------

ARG PYTHON_VERSION=3.12

# --- Stage 1: build dependencies -------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential covers any dependency without a prebuilt wheel for this
# platform; it is discarded with this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt


# --- Stage 2: the application image -----------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS app-base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is used by the container healthcheck; tini reaps zombies and forwards
# signals so SIGTERM reaches the app for a graceful shutdown.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY --chown=app:app alembic.ini pyproject.toml ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app app ./app

RUN chmod +x scripts/entrypoint.sh

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "scripts/entrypoint.sh"]
CMD ["api"]


# --- Stage 3: test image ----------------------------------------------------
# Production image + test tooling + the suite. Because it starts FROM app-base
# there is no way for the tested runtime to drift from the shipped one.
FROM app-base AS test

USER root

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY --chown=app:app tests ./tests

# Writable cache locations for pytest/ruff/mypy, which run as the app user.
ENV RUFF_CACHE_DIR=/tmp/ruff \
    MYPY_CACHE_DIR=/tmp/mypy \
    PYTEST_ADDOPTS=""

USER app

# Plain entrypoint so `pytest`, `ruff` and `mypy` can be invoked directly.
ENTRYPOINT []
CMD ["pytest"]


# --- Stage 4: production runtime (default target) ---------------------------
# Last stage, so `docker build .` with no --target yields production, not test.
FROM app-base AS runtime
