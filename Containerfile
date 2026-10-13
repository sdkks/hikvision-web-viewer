# --- builder: install runtime dependencies from the committed lock ----------
# uv (and its cache) live only in this stage; the runtime image gets just the
# resolved venv, so it never carries uv, a compiler, or the dev gate tools.
FROM python:3.14-slim AS builder

# Pin uv so the lock is read by a known tool version.
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Runtime dependencies only: --no-dev skips the gate toolchain (mypy, ruff,
# pre-commit, types-requests); --no-install-project keeps this application out
# of its own venv (it is run from source, not installed).
RUN uv sync --frozen --no-dev --no-install-project

# --- runtime -----------------------------------------------------------------
FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg procps \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

COPY viewer/ viewer/
COPY static/ static/

ENV VIEWER_BIND=0.0.0.0
EXPOSE 8756

CMD ["python", "-m", "viewer.app"]
