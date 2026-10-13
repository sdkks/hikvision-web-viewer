# Contributing

Thanks for helping improve `hikvision-web-viewer`! This is a small, focused
tool: local viewing of Hikvision cameras without the vendor cloud. Contributions
that keep it simple, honest, and dependency-light are very welcome.

Before diving in, read [AGENTS.md](AGENTS.md) — it documents the architecture,
the invariants (especially the "no viewer = no ffmpeg" lifecycle and the codec
auto-detection), and a testing playbook.

## Requirements

- Python 3.9+
- `ffmpeg` on `PATH` (streams are remuxed/transcoded by ffmpeg)
- A reachable Hikvision camera for real testing (optional: mock mode works
  without one)
- Optional: `podman` + `podman-compose` for container testing

## Setup

```bash
git clone <this repo> && cd hikvision-web-viewer
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Install the git hooks — REQUIRED before your first commit
venv/bin/pre-commit install

# Local configuration (gitignored; never commit credentials)
cp config.env.example config.env
$EDITOR config.env
```

## Running

```bash
# No camera needed — deterministic fixtures
HIK_MOCK=1 VIEWER_PORT=8757 venv/bin/python -m viewer.app

# Real camera
set -a; source config.env; set +a
venv/bin/python -m viewer.app            # http://127.0.0.1:8756
```

## Gates — run these before every PR

```bash
venv/bin/ruff check .                       # lint + import order
venv/bin/ruff format --check .              # formatting
venv/bin/python -B -m unittest discover -s tests
venv/bin/pre-commit run -a                  # runs all of the above + gitleaks
```

The pre-commit hooks also enforce **Conventional Commits**
(`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, …) on the commit
message. If `pre-commit run -a` passes on a clean tree, your PR will pass CI-style
review of those gates.

## Testing your change

- **Unit tests** — add or extend tests in `tests/test_viewer.py` for pure logic
  (time math, URI parsing, error mapping, auth). Keep the suite fast.
- **Streams** — verify with `curl` + `ffprobe` without a browser; the recipes are
  in AGENTS.md ("Testing playbook"). Confirm the output codec is H.264 and that
  session directories get an `ffmpeg-stderr.log`.
- **Browser** — when touching `static/app.js` or playback behaviour, check
  Firefox, Chrome, and Safari (including a phone if you can): live view,
  channel switching, recordings search, clip playback, and the tab-hidden state
  (no ffmpeg should remain when nobody is watching).
- **Resource behaviour** — `pgrep -f ffmpeg` should drop to zero within a few
  seconds of closing/hiding the viewer, and idle sessions should be reaped.

## Guidelines

- **Keep it dependency-light.** No JS framework, no build step, no new runtime
  dependency without a strong reason. The HTTP server is Python's stdlib.
- **Keep the UI honest.** Distinguish "camera unreachable", "service down", and
  "no recordings". Never present a claim the UI cannot verify (e.g. hardcoded
  latency). Snapshot stills remain the fallback everywhere.
- **Respect the session lifecycle.** Any long-running subprocess must use the
  watchdog spawn wrapper, register with the session registry, be group-killed,
  and be stopped when no viewer is present.
- **Codec handling stays automatic.** H.264 copies; everything else transcodes.
  Do not hardcode a codec in the ffmpeg builders.
- **Never commit secrets.** Credentials belong in `config.env` (gitignored) or
  environment variables. `gitleaks` blocks accidental commits.
- **Update the docs.** If you change configuration or behaviour, update
  `README.md` and `AGENTS.md`.

## Continuous integration

Every push and pull request runs the gates on **Ubuntu 26.04 (amd64) and
Ubuntu 26.04 (arm64)** (`ubuntu-26.04`, `ubuntu-26.04-arm` — free for public
repos), plus an integration smoke test (`scripts/ci-smoke.sh`) that starts the
server in mock mode, plays a real HLS session, checks the segment is H.264,
exercises the stop route, and asserts no ffmpeg survives.

Run the same smoke test locally:

```bash
HIK_MOCK=1 VIEWER_PORT=8799 venv/bin/python -m viewer.app &
VIEWER_PORT=8799 bash scripts/ci-smoke.sh
```

Pushing a `v*` tag also builds and publishes multi-arch container images to
GHCR (`.github/workflows/container.yml`):

- `ghcr.io/sdkks/hikvision-web-viewer:<version>-amd64` / `-arm64` (per-arch)
- `ghcr.io/sdkks/hikvision-web-viewer:<version>` and `:latest` (multi-arch manifests)
- `:edge` / `:edge-amd64` / `:edge-arm64` from `main`

For a local publish, use `make docker-publish` (podman manifest, tags a version
and the current commit).

Auth notes: in Actions, `secrets.GITHUB_TOKEN` with `packages: write` is enough
to push to GHCR — no PAT required. For local publishing, `podman login ghcr.io`
with a token that has `write:packages`. New GHCR packages start **private**;
flip visibility to public once in the package settings if you want anonymous
pulls.


## Pull requests

Fill in the PR template — it includes the checklist confirming that hooks are
installed and the full gates were run. Small, focused PRs with a clear
description and evidence (commands you ran, output you observed) are easiest to
review.

When reporting a streaming or ffmpeg problem, include:

- camera model + firmware, and whether `HIK_MOCK` was involved
- what you saw in the browser and the status-line text
- `ps -eo pid,ppid,pcpu,etime,command | grep ffmpeg` output for stray processes
- the relevant session `ffmpeg-stderr.log`

## Security notes

- The viewer itself supports optional HTTP Basic Auth
  (`VIEWER_AUTH_USER` / `VIEWER_AUTH_PASSWORD`). Basic Auth is plaintext over
  HTTP — terminate TLS in a reverse proxy if you expose it beyond your LAN.
- The app is designed for a trusted LAN. Report security issues privately to
  the maintainer rather than opening a public issue with exploitable details.
