.PHONY: install test lint format gates run run-mock docker-build docker-publish

PY ?= venv/bin/python
DOCKER_IMAGE ?= sdkks/hikvision-web-viewer
DOCKER_PLATFORMS ?= linux/amd64,linux/arm64
VERSION ?= edge
HOST_PORT ?= 8756

install:
	python3 -m venv venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install ruff pre-commit
	venv/bin/pre-commit install

lint:
	$(PY) -m ruff check .

format:
	$(PY) -m ruff format .

test:
	$(PY) -B -m unittest discover -s tests -v

# Full local gate set (what CI runs plus the pre-commit suite).
gates:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -B -m unittest discover -s tests
	venv/bin/pre-commit run -a

run:
	$(PY) -m viewer.app

run-mock:
	HIK_MOCK=1 VIEWER_PORT=8757 $(PY) -m viewer.app

# Single-arch local build for the current platform.
docker-build:
	podman build -f Containerfile -t $(DOCKER_IMAGE):local .

# Multi-arch publish to GHCR (podman manifest, same convention as sdkks/7b-mta).
# Requires: podman login ghcr.io  (GHCR token with write:packages)
docker-publish:
	$(eval COMMIT_SHA := $(shell git rev-parse --short=12 HEAD))
	podman build --no-cache --platform $(DOCKER_PLATFORMS) --manifest ghcr.io/$(DOCKER_IMAGE):$(VERSION) -f Containerfile .
	podman manifest push --all ghcr.io/$(DOCKER_IMAGE):$(VERSION) docker://ghcr.io/$(DOCKER_IMAGE):$(VERSION)
	podman manifest push --all ghcr.io/$(DOCKER_IMAGE):$(VERSION) docker://ghcr.io/$(DOCKER_IMAGE):$(COMMIT_SHA)
	@echo "Published ghcr.io/$(DOCKER_IMAGE):$(VERSION) and :$(COMMIT_SHA) for $(DOCKER_PLATFORMS)"
