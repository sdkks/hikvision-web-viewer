.PHONY: setup install test lint format typecheck gates run run-mock docker-build docker-publish

PYTHON_VERSION ?= 3.14
VENV ?= .venv
PY ?= $(VENV)/bin/python
DOCKER_IMAGE ?= sdkks/hikvision-web-viewer
DOCKER_PLATFORMS ?= linux/amd64,linux/arm64
VERSION ?= edge

# One-shot developer setup: ensures uv + direnv exist, creates the uv-managed
# Python 3.14 venv, installs runtime + dev dependencies, and git hooks.
setup:
	@command -v uv >/dev/null 2>&1 || { \
		if command -v brew >/dev/null 2>&1; then brew install uv; \
		else echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; fi; }
	@command -v direnv >/dev/null 2>&1 || { \
		if command -v brew >/dev/null 2>&1; then brew install direnv; \
		else echo "direnv is required: https://direnv.net/docs/installation.html"; exit 1; fi; }
	uv venv $(VENV) --python $(PYTHON_VERSION) --seed
	uv pip install --python $(PY) -r requirements.txt -r requirements-dev.txt ruff pre-commit
	$(VENV)/bin/pre-commit install
	@echo ""
	@echo "Setup complete."
	@echo "  - Run 'direnv allow' once to auto-load .venv and config.env"
	@echo "  - Run 'make gates' before committing"

install: setup

test:
	$(PY) -B -m unittest discover -s tests -v

lint:
	$(PY) -m ruff check .

format:
	$(PY) -m ruff format .

typecheck:
	$(PY) -m mypy --config-file pyproject.toml

# Full local gate set (what CI checks, plus the pre-commit suite).
gates:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy --config-file pyproject.toml
	$(PY) -B -m unittest discover -s tests
	$(VENV)/bin/pre-commit run -a

run:
	$(PY) -m viewer.app

run-mock:
	HIK_MOCK=1 VIEWER_PORT=8757 $(PY) -m viewer.app

# Single-arch local build for the current platform.
docker-build:
	podman build -f Containerfile -t $(DOCKER_IMAGE):local .

# Multi-arch publish to GHCR (podman manifest, same convention as sdkks/7b-mta).
# Requires: podman login ghcr.io  (token with write:packages)
docker-publish:
	$(eval COMMIT_SHA := $(shell git rev-parse --short=12 HEAD))
	podman build --no-cache --platform $(DOCKER_PLATFORMS) --manifest ghcr.io/$(DOCKER_IMAGE):$(VERSION) -f Containerfile .
	podman manifest push --all ghcr.io/$(DOCKER_IMAGE):$(VERSION) docker://ghcr.io/$(DOCKER_IMAGE):$(VERSION)
	podman manifest push --all ghcr.io/$(DOCKER_IMAGE):$(VERSION) docker://ghcr.io/$(DOCKER_IMAGE):$(COMMIT_SHA)
	@echo "Published ghcr.io/$(DOCKER_IMAGE):$(VERSION) and :$(COMMIT_SHA) for $(DOCKER_PLATFORMS)"
