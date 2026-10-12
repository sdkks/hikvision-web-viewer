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

- Python 3.9+
- ffmpeg on PATH (streams are `-c copy`; no transcoding)
- A reachable Hikvision camera with ISAPI/RTSP enabled (tested on DS-2CV2121G2-IDW)

## Quick start

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.env.example config.env   # then edit credentials
source config.env && export HIK_HOST HIK_USER HIK_PASSWORD
venv/bin/python -m viewer.app
```

Open http://127.0.0.1:8756

No camera at hand? `HIK_MOCK=1 venv/bin/python -m viewer.app` runs against built-in fixtures.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `HIK_HOST` | `192.168.0.156` | Camera address |
| `HIK_USER` / `HIK_PASSWORD` | `admin` / empty | ISAPI/RTSP digest credentials |
| `HIK_RTSP_PORT` / `HIK_HTTP_PORT` | `554` / `80` | Camera service ports |
| `HIK_MOCK` | unset | `1` = built-in fixtures, no device needed |
| `VIEWER_BIND` / `VIEWER_PORT` | `127.0.0.1` / `8756` | Server bind address |

Credentials are read from the environment only — never committed. `config.env` is gitignored.

## Browser support

Live/playback video uses MediaSource Extensions (ManagedMediaSource on iOS 17.1+). The camera's
main stream is H.265: Safari (macOS/iOS) decodes it; Chrome needs the sub channel or falls back to
snapshot stills (the UI says so). Still-image links work everywhere, JavaScript or not.

## Notes on honesty (by design)

- Playback runs at the camera's tempo: a 20-minute clip takes 20 minutes; there is no seeking.
- The camera allows **one** playback session; starting a new one takes over the old.
- Recording search is served by the camera's app layer, which on some firmware is unreliable —
  the UI names that state instead of pretending it is "no clips".

## Development

```bash
venv/bin/pre-commit install          # gitleaks, ruff, unittest, conventional commits
venv/bin/python -B -m unittest discover -s tests -v
venv/bin/ruff check . && venv/bin/ruff format --check .
```

Layout: `viewer/camera.py` (ISAPI client + error model), `viewer/streams.py` (ffmpeg sessions),
`viewer/app.py` (HTTP server + pages), `static/` (CSS + streaming client), `tests/`.
