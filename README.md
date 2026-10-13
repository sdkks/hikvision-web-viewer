# hikvision-web-viewer

A small, self-hosted web app to view a Hikvision camera **without the vendor cloud or app**.
Shows live view and recorded clips in the browser, honest about what the camera can and cannot do.

- **Live view** — main (1080p) and sub (360p) channels, streamed as fragmented MP4 via ffmpeg.
- **Recordings browser** — pick a range (presets or exact date/time), see motion clips, play them
  in the browser at the camera's pace, with copyable rtsp/ffmpeg escape hatches.
- **Honest states** — the camera's search service is known to be flaky; the UI distinguishes
  "search down" from "no clips" from "camera unreachable", and always offers snapshot stills as a
  floor. No vendor cloud, no accounts.

## Requirements

- Python 3.14+
- ffmpeg on PATH (streams are `-c copy`; no transcoding)
- A reachable Hikvision camera with ISAPI/RTSP enabled (tested on DS-2CV2121G2-IDW)

## Quick start

```bash
make setup                          # uv + direnv, .venv on Python 3.14, hooks
cp config.env.example config.env    # then edit credentials
direnv allow                        # loads .venv and config.env automatically
.venv/bin/python -m viewer.app
```

`make setup` installs `uv` and `direnv` via Homebrew if they are missing, creates
the uv-managed `.venv` (Python 3.14), installs dependencies and the git hooks.
`direnv` (via the committed `.envrc`) activates `.venv` and sources `config.env`
on `cd`, so no manual exports are needed.

Open http://127.0.0.1:8756

No camera at hand? `HIK_MOCK=1 .venv/bin/python -m viewer.app` runs against built-in fixtures.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `HIK_HOST` | `192.168.0.156` | Camera address |
| `HIK_USER` / `HIK_PASSWORD` | `admin` / empty | ISAPI/RTSP digest credentials |
| `HIK_RTSP_PORT` / `HIK_HTTP_PORT` | `554` / `80` | Camera service ports |
| `HIK_MOCK` | unset | `1` = built-in fixtures, no device needed |
| `VIEWER_BIND` / `VIEWER_PORT` | `127.0.0.1` / `8756` | Server bind address |
| `VIEWER_AUTH_USER` / `VIEWER_AUTH_PASSWORD` | empty | When both are set, HTTP Basic Auth is required for the whole viewer |

Credentials are read from the environment only — never committed. `config.env` is gitignored.

### Viewer authentication (optional)

Set `VIEWER_AUTH_USER` and `VIEWER_AUTH_PASSWORD` and the viewer answers `401`
until credentials are supplied, so `http://user:pass@host:8756/` works in a
browser (or any HTTP client with Basic Auth). Basic Auth sends credentials in
plaintext over HTTP — put a TLS-terminating reverse proxy in front if the tool
is exposed beyond a trusted LAN.

## Codec handling (H.264, H.265/HEVC, H.265+)

The server auto-detects each channel's encoding via ISAPI at session start:

- **H.264 source** -> remuxed into HLS verbatim (`-c copy`) — near-zero CPU.
- **H.265/HEVC source** (including H.265+/Smart codec streams) -> transcoded to
  H.264 on the fly, so every browser plays it (Firefox and Chrome cannot MSE-decode
  HEVC reliably). Costs CPU only while someone is actually watching.

Playback uses hls.js on every browser (native HLS on Safari as fallback), so both
codecs work regardless of vendor. Camera-side you can freely choose H.264, H.265
or H.265+ — the viewer adapts automatically.

The codec is read at session start, so after changing the camera's encoding the
new mode applies the next time a stream starts (close/hide the tab, or wait for
the idle reaper).

**Live latency follows the camera's I-frame interval.** In copy mode HLS segments
can only be cut at keyframes: a 50-frame GOP at 12 fps gives ~4 s segments and a
~12 s live delay. Set the camera's I-frame interval to 1-2 s worth of frames
(e.g. 12-24 at 12 fps) for a snappier live view. The status line shows the
measured latency, whatever it is. Still-image links work everywhere,
JavaScript or not.

## Notes on honesty (by design)

- Playback runs at the camera's tempo: a 20-minute clip takes 20 minutes; there is no seeking.
- The camera allows **one** playback session; starting a new one takes over the old.
- Recording search is served by the camera's app layer, which on some firmware is unreliable —
  the UI names that state instead of pretending it is "no clips".

## Container image (GHCR)

Prebuilt multi-arch images are published on version tags:

```bash
podman run -d --name camera-viewer -p 8756:8756 --env-file config.env \
  ghcr.io/sdkks/hikvision-web-viewer:latest
# per-arch tags: :latest-amd64 / :latest-arm64 (or :<version>-amd64/-arm64)
# rolling main builds: :edge / :edge-amd64 / :edge-arm64
```

## Run with podman (NAS / always-on box)

```bash
cp config.env.example config.env   # edit: camera address + credentials
podman build -f Containerfile -t hikvision-web-viewer .
podman run -d --name camera-viewer -p 8756:8756 --env-file config.env hikvision-web-viewer
# or: podman-compose up -d --build   (reads compose.yaml + config.env)
```

Resource notes: the HEVC→H.264 transcode costs CPU **only while a stream is
actually being watched** — the page stops its session when the tab is hidden or
closed, and idle sessions are reaped automatically. On a weak NAS CPU, set
`VIEWER_DEFAULT_CHANNEL=102` to make the 360p sub channel the default live view.

## Development

```bash
.venv/bin/pre-commit install          # gitleaks, ruff, unittest, conventional commits
.venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Layout: `viewer/camera.py` (ISAPI client + error model), `viewer/streams.py` (ffmpeg sessions),
`viewer/app.py` (HTTP server + pages), `static/` (CSS + streaming client), `tests/`.

## License

MIT — see [LICENSE](LICENSE).
