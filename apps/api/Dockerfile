# DroneOps API image: multi-stage, non-root, no aircraft command path.
# Pin these digests in a release build; the tags here are the reviewed baseline.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Lockfile and package metadata first so dependency layers cache.
COPY pyproject.toml uv.lock ./
COPY apps ./apps
COPY packages ./packages
COPY adapters ./adapters

# Runtime dependencies only: no dev tools, no test runners, no editable installs.
RUN uv sync --frozen --no-dev --no-editable --package droneops-api

FROM python:3.13-slim AS runtime

RUN groupadd --gid 10001 droneops \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin droneops

COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DRONEOPS_SIMULATION_ONLY=1 \
    HOME=/home/droneops

WORKDIR /app
# Nothing here writes to disk and nothing here reaches a vehicle.
RUN chown -R droneops:droneops /app
USER droneops

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "droneops_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
