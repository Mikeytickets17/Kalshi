# Dashboard container for Fly.io.
#
# Single-stage, Python 3.11 slim. Only installs runtime deps (not
# pytest/mypy/ruff). Image ends up around 150MB which fits comfortably
# on the shared-cpu-1x / 256MB machine size.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install OS deps needed for libsql (libsql-experimental ships prebuilt
# wheels for manylinux so this is cheap).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy the package + pyproject so pip resolves the runtime deps.
COPY pyproject.toml ./
COPY kalshi_arb ./kalshi_arb

# Install only runtime dependencies. Dev extras ([dev]) stay out of
# the image.
RUN pip install --upgrade pip && pip install .

# Non-root user for defense-in-depth.
RUN useradd --uid 1001 --create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

ENV PORT=8080
EXPOSE 8080

# Fly sends SIGINT for graceful shutdown; uvicorn respects it.
CMD ["uvicorn", "kalshi_arb.dashboard.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--log-level", "info", "--no-access-log"]
