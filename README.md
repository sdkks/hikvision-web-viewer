# hikvision-web-viewer

A small, self-hosted web app to view a Hikvision camera **without the vendor cloud or app**.
Shows live view and recorded clips in the browser, honest about what the camera can and cannot do.

- **Live view** — main (1080p) and sub (360p) channels, streamed as fragmented MP4 via ffmpeg.
- **Recordings** — presets or exact range list the files on the card, plus a **browse-by-time picker**
  that opens on today: see which hours have recordings, drill into an hour to see the recorded
  minutes, and play or download from any point. An always-available **Refresh recordings** action
  bypasses the cached day (so a clip recorded a minute ago appears without reloading the page; a
  failed refresh keeps the previous view instead of blanking it), and leaving a clip returns you to
  the same day and hour. Copyable rtsp/ffmpeg escape hatches included.
- **Motion zones** — read the camera's motion detection configuration and paint which cells count as
  motion: an 18 x 22 grid drawn over a live still, an Arm/Mask brush with drag painting, a 12-zone
  quick map for small screens, a sensitivity control, and an on/off switch. Applying writes the
  merged document and then **reads it back** — the page reports what the camera stored, and says so
  when the camera changes or ignores something. Zones can be painted while detection is off, with
  the page stating plainly that they do nothing until it is on.
- **Restart from anywhere in a clip** — the player carries a bar spanning the clip, shaded with what
  was actually recorded (reusing the picker's coverage vocabulary, with an explicit "couldn't check"
  state), so you can see where video exists. Drag or tap to preview, then **Restart here** to begin a
  new session from that time; browser Back returns you to where you were. It is deliberately not a
  seek — the camera plays linearly and cannot seek inside a session, and it starts at the nearest
  keyframe before the time you pick (a few seconds earlier).
- **Jump between clips** — the player lists the five earlier and five later recordings, each labelled
  with the gap a jump would skip (so a jump across missing time never looks continuous), and can
  chain to the next recording automatically. That auto-advance is opt-in, counted down visibly, and
  cancellable.
- **Honest states** — the camera's search service is known to be flaky; the UI distinguishes
  "search down" from "no clips" from "camera unreachable", and always offers snapshot stills as a
  floor. A refused credential is named as exactly that rather than reported as a camera that did not
  answer. No vendor cloud, no accounts.

## Requirements

- Python 3.14+
- ffmpeg on PATH (streams are `-c copy`; no transcoding)
- A reachable Hikvision camera with ISAPI/RTSP enabled and **digest authentication** turned on
  (this firmware refuses Basic auth with `401`) — tested on DS-2CV2121G2-IDW, firmware V5.7.26

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
| `VIEWER_ALLOWED_HOSTS` | empty | Extra host names accepted in the `Host` header (comma-separated, ports ignored) — see below |
| `TZ` | host | Display timezone. Containers default to UTC — set it (e.g. `Asia/Singapore`) or all times and day boundaries shift |

Credentials are read from the environment only — never committed. `config.env` is gitignored.

### Viewer authentication (optional)

Set `VIEWER_AUTH_USER` and `VIEWER_AUTH_PASSWORD` and the viewer answers `401`
until credentials are supplied, so `http://user:pass@host:8756/` works in a
browser (or any HTTP client with Basic Auth). Basic Auth sends credentials in
plaintext over HTTP — put a TLS-terminating reverse proxy in front if the tool
is exposed beyond a trusted LAN.

### Reaching the viewer by name (`VIEWER_ALLOWED_HOSTS`)

Requests that write to the camera — the motion-zone editor's Apply — are only
accepted when the `Host` header names the viewer itself. `localhost`,
`127.0.0.1`, `::1` and a specific `VIEWER_BIND` address always work, so an
ordinary local setup needs no configuration.

If you reach the viewer by any other name — a LAN hostname, or through a reverse
proxy — list that host (comma-separated, ports ignored):

```bash
VIEWER_ALLOWED_HOSTS=viewer.local,cam.lan
```

A request presenting an unrecognised host is refused with a message naming that
variable, even when its `Origin` matches. That is deliberate: comparing `Origin`
with `Host` alone is satisfied by a DNS-rebinding page, which controls both
values, and this endpoint changes what your camera treats as motion.

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
  The player's position readout is therefore an **estimate** (`≈`), derived from wall-clock playback
  time, and it is hidden while playback is paused or stalled rather than showing a confident number;
  it can run a few seconds ahead of the picture, because the browser buffers behind the stream.
- The camera allows **one** playback session; starting a new one takes over the old.
- Recording search is served by the camera's app layer, which on some firmware is unreliable —
  the UI names that state instead of pretending it is "no clips".

## What this firmware exposes (verified on the device)

Everything below was confirmed against a real DS-2CV2121G2-IDW running V5.7.26. It
is what the viewer depends on, and it is the part that is most likely to differ on
other models.

- **Digest authentication is mandatory.** Every ISAPI request must use HTTP digest;
  Basic auth is answered with `401`.
- **Motion detection** is at
  `GET/PUT /ISAPI/System/Video/inputs/channels/1/motionDetection` and is fully readable
  *and* writable. The detection area is a grid of **18 rows by 22 columns**,
  serialised as a 108-character hex `gridMap` (6 hex characters per row, the low two
  bits of each row unused). Per Hikvision's ISAPI IPMD guide, *the first cell is the
  upper-left corner, and the order then runs left to right and top to bottom*, with
  `1` meaning "detect here" and `0` meaning "ignore".
- **Sensitivity** is 0-100 in steps of 20, so only `0/20/40/60/80/100` mean anything.
  Any other value is accepted with `HTTP 200` and **silently ignored** (the camera
  keeps its previous level) — which is exactly why the viewer reads the value back
  after every change instead of trusting the request.
- **Writing means sending the whole document back.** The viewer reads the current
  document, changes only the fields it owns, and PUTs the merged result. The XML must
  keep the default namespace (`xmlns="http://www.hikvision.com/ver20/XMLSchema"`); a
  body serialised with `ns0:` prefixes is rejected with `400`.
- **Detection and recording mode are separate.** `/ISAPI/ContentMgmt/record/tracks`
  shows the track schedule, and that schedule is where the recording mode lives: this
  camera shipped recording continuously (`CMR`) around the clock, which is why the older
  clips are large. Each day's window can instead be set to `MOTION`, after which a clip
  begins only when motion lands in an armed zone (with the 5 s pre/post roll configured on
  the track). Turning motion detection on does **not** by itself make recording
  motion-triggered — that is a change to the track's schedule, and the viewer does not make
  it for you.
- **Unsupported on this firmware:** `/ISAPI/Event/triggers` and the record-plan
  endpoints answer `403 Invalid Operation`, and the device's event stream carries
  interface noise rather than motion events.
- **ONVIF is off on this unit and unused by the viewer.** With ONVIF enabled, the
  camera advertises device/media/events/analytics services but **no motion service**,
  so zone configuration stays on ISAPI either way.

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
`viewer/app.py` (HTTP server + pages), `viewer/motion.py` (motion grid encode/decode + zone
operations, pure), `static/` (CSS + streaming client), `tests/`.

## License

MIT — see [LICENSE](LICENSE).
