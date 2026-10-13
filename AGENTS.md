# AGENTS.md

Orientation for agents (and humans) working **on** `hikvision-web-viewer`.

This file explains how the system works, what must not break, and how to test
changes. It is written to be read before touching code.

---

## What this project is

A self-hosted, single-user web app that views a Hikvision camera **without the
vendor cloud or app**:

- **Live view** — main/sub channel, streamed as HLS (fragmented TS) to the browser
- **Recordings** — ISAPI search over the SD card, in-browser playback, clip download
- **Downloads** — full clips over HTTP via ISAPI's `ContentMgmt/download` (~4x realtime)
- **Motion zones** — read/paint the camera's motion-detection grid (18x22) over a still,
  set sensitivity, toggle detection, apply with a readback
- No cloud dependency, no accounts, LAN-only by default

## Request flow (how a frame reaches the screen)

```
Camera ──ISAPI (HTTP digest)──> viewer/camera.py        device info, status,
   │                             search, snapshots, codec detection
   │
   └──RTSP──> viewer/streams.py ── ffmpeg ──> HLS (.ts + .m3u8) ──> viewer/app.py
                  │  (copy or transcode, per channel codec)         routes /hls/*
                  └─ session dir under $TMPDIR/hikvision-viewer-hls-<port>/
                                                                     │
Browser: static/app.js (hls.js) ────────────────────────────────────>┘
```

### File map

| Path | Responsibility |
|---|---|
| `viewer/camera.py` | ISAPI client, error model, `MockCamera` fixtures, codec/resolution detection |
| `viewer/streams.py` | ffmpeg HLS session manager: spawn/kill, codec decision, watchdog, reaper |
| `viewer/app.py` | stdlib `ThreadingHTTPServer`: routes, auth gate, page rendering |
| `viewer/templates.py` | Server-rendered HTML (string templates, no framework) |
| `viewer/config.py` | Environment configuration (single source of truth) |
| `viewer/util.py` | Time/formatting/URI helpers (camera UTC <-> local time) |
| `viewer/coverage.py` | Aggregates a day of clips into coverage buckets for the picker (pure) |
| `viewer/motion.py` | Motion grid encode/decode + zone operations (pure); the single source of bit order |
| `static/app.js` | hls.js player, status states, visibility/pagehide lifecycle |
| `static/style.css` | Dark, mobile-first design tokens |
| `tests/test_viewer.py` | Unit tests (time math, parsing, auth, mock camera, motion grid + API guard) |
| `scripts/ci-smoke.sh` | Integration smoke test used by CI (mock session, HLS, stop, no-ffmpeg) |
| `.github/workflows/ci.yml` | Gates on ubuntu-26.04 amd64 + arm64 |
| `.github/workflows/container.yml` | Multi-arch container publish to GHCR on tags |
| `Makefile` | setup / gates / run-mock / docker-build / docker-publish |
| `.envrc` | direnv: activates `.venv` and sources `config.env` if present |

---

## Invariants — do not break these

### 1. No viewer = no ffmpeg

Running a transcode for nobody is the cardinal sin of this project. Four layers
enforce it; keep all of them working when you touch sessions:

1. **Stop routes** — `/hls-live-stop?ch=101` and `/hls-play-stop` (GET or POST;
   `sendBeacon` needs POST) kill a session immediately.
2. **Visibility lifecycle** (`static/app.js`) — `visibilitychange` (tab hidden)
   and `pagehide` (tab closed) destroy the player and call the stop routes.
3. **Idle reaper** (`streams._reap_loop`) — kills sessions not polled for
   `LIVE_IDLE_S` / `PLAY_IDLE_S` seconds; backstop for crashed browsers.
4. **Parent watchdog** (`streams._spawn`) — every ffmpeg runs under
   `/bin/sh` that watches the server PID and SIGKILLs ffmpeg if the server dies
   (macOS has no `PR_SET_PDEATHSIG`). Sessions spawn in their own process
   group; `_kill()` signals the **group** (TERM, then KILL).

Any new long-running subprocess must follow the same pattern. Never spawn a
codec/binary directly.

### 2. Codec handling is automatic

`camera.stream_codec(channel)` reads `videoCodecType` via ISAPI at session start:

- **H.264** source -> `-c copy` (remux only, ~0.1% CPU)
- **H.265 / HEVC / H.265+ / unknown** -> libx264 transcode (web-safe output)

`streams._hls_out(..., transcode=...)` is the single place that decides. Do not
hardcode `libx264` in command builders. H.265+ is a bitrate scheme on top of
standard HEVC — treat it as HEVC.

**Live latency is bounded by the camera's I-frame interval** in copy mode (HLS
segments can only start at keyframes). The UI displays measured latency
(`hls.latency`), never a hardcoded claim.

### 3. The camera is flaky — the UI must stay honest

This firmware (DS-2CV2121G2-IDW, V5.7.26) drops its application services
per boot: search/storage/deviceInfo can be down while RTSP and snapshots keep
working. Use the error model:

- `UnreachableError` -> "Camera unreachable at <host>"
- `ServiceDownError` -> "search service isn't responding … live view still works"
- `BadResponseError` -> unexpected reply (firmware drift)

Never collapse "service down" into "no recordings" — distinguishing them *is*
the feature. Streaming/snapshots are the reliable floor; offer them everywhere.

**A control that displays camera state must render an explicit unknown state
until the camera has actually answered.** Never pre-paint a plausible-looking
default: a viewer that shows an all-masked zone map before (or without) a
successful read is worse than one showing nothing, because the wrong answer
looks exactly like a real one.

**Clip position is never derived from `<video>.currentTime`.** The playback
playlist is live-ish (`-hls_list_size`), so `currentTime`/`duration` do not map
to clip time — a width read from them is a fabricated position. Progress comes
from wall-clock elapsed time since the session started playing, is displayed as
an estimate (`≈`), and is hidden whenever playback is not actively running. The
same applies to restarting: the camera cannot seek inside a session, so "go to a
time" is a new `/play` navigation (which is also why browser Back can undo it),
and it lands on the nearest keyframe before the requested time.

### 4. The coverage picker stays honest

`/api/coverage?date=YYYY-MM-DD` runs **one search per day** and returns
`state` (`ok|empty|truncated|service_down|unreachable|bad_response`) plus hour
and minute levels and merged ranges. The picker maps each state to distinct
copy: "no recordings" is neutral; "service down" is a warning with Retry.
Never let an unchecked day render as an empty day. The client caches per date
and deep-links as `?date=&hour=&min=` (browser Back steps up a level).

### 5. Secrets stay out of the repo

Camera credentials and viewer auth live only in `config.env` (gitignored) or
environment variables. `gitleaks` runs in pre-commit and will fail the commit.
Never hardcode credentials in source, tests, docs, or compose files.

### 6. The motion write is the only mutating endpoint

`POST /api/motion` changes state on the camera, so it carries obligations the rest of
the app does not. Keep all five when touching it:

1. **Same-origin only.** With Basic Auth a browser attaches credentials automatically,
   so a cross-site form could otherwise drive it. Reject a mismatched `Origin` (when
   present) and require a JSON content type. That pair alone is not enough: a
   DNS-rebinding page controls both `Origin` and `Host`, so the request is also
   refused unless `Host` names the viewer (loopback names, a specific `VIEWER_BIND`
   address, or an entry in `VIEWER_ALLOWED_HOSTS`). Keep that allowlist tight and
   name the variable in the refusal so a LAN or reverse-proxy user can fix it.
2. **Read-modify-write.** GET the camera's document, change only the fields the viewer
   owns, PUT the merged document back — never hand-build a minimal body.
3. **The readback is the truth.** After writing, GET again and report what the camera
   actually stored, including drift: this firmware accepts a sensitivity outside the
   six valid steps with `200` and silently keeps the old value. Never report success
   from the request alone, and never report an unobserved failure either — on a PUT
   timeout, re-GET and compare before saying anything.
4. **Namespace.** Register the default XML namespace before serialising
   (`ET.register_namespace("", "http://www.hikvision.com/ver20/XMLSchema")`); a body
   emitted with `ns0:` prefixes is rejected with `400`.
5. **One source of bit order.** All grid encode/decode lives in `viewer/motion.py`,
   following the vendor rule: first cell upper-left, then left to right, then top to
   bottom, `1` = detect. Two documented assumptions live there — rows run from the top
   of the image, and the 18x22 grid spans the whole streamed frame (`GRID_SPANS_FRAME`).
   Keep them visible rather than burying them.

---

## Configuration

All configuration is environment-based (`viewer/config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `HIK_HOST` | `192.168.0.156` | Camera address |
| `HIK_USER` / `HIK_PASSWORD` | `admin` / empty | Camera ISAPI + RTSP credentials |
| `HIK_RTSP_PORT` / `HIK_HTTP_PORT` | `554` / `80` | Camera service ports |
| `HIK_MOCK` | unset | `1` = built-in fixtures, no camera required |
| `VIEWER_BIND` / `VIEWER_PORT` | `127.0.0.1` / `8756` | Server bind address |
| `VIEWER_DEFAULT_CHANNEL` | `101` | Default live channel (`102` on weak CPUs) |
| `VIEWER_AUTH_USER` / `VIEWER_AUTH_PASSWORD` | empty | When both set, HTTP Basic Auth is required for everything |
| `VIEWER_ALLOWED_HOSTS` | empty | Extra host names accepted in the `Host` header (comma-separated, ports ignored) |

`config.env.example` documents the format; `config.env` is the local copy.

---

## Development workflow

```bash
make setup
.venv/bin/pre-commit install            # gitleaks, ruff, format, unittest, commit-msg
cp config.env.example config.env       # then edit credentials
```

Run (mock mode needs no camera):

```bash
HIK_MOCK=1 VIEWER_PORT=8757 .venv/bin/python -m viewer.app     # fixtures
source config.env && export HIK_HOST HIK_USER HIK_PASSWORD
.venv/bin/python -m viewer.app                                 # real camera
```

Gates (all must pass before committing):

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -B -m unittest discover -s tests
.venv/bin/pre-commit run -a
```

Commit messages are Conventional Commits (`feat:`, `fix:`, `docs:`, …) — the
`commit-msg` hook enforces it.

---

## Testing playbook

### Unit tests

`tests/test_viewer.py` covers time conversion, playback-URI parsing, the error
model, auth, and the mock camera. Add tests for any pure-logic change. The suite
must stay fast (<1s) and dependency-free beyond `requests`.

**Tests never write to a real camera.** The device is read-only for the suite and for
any agent verifying a change: GET requests are fine, every write path is exercised
against `MockCamera` / `HIK_MOCK=1`. The motion grid has one vendor-documented test
vector in the suite (108 hex characters from Hikvision's ISAPI IPMD guide) so the bit
order can never drift silently.

### Verifying streams by hand (no browser needed)

```bash
# Start a live session and fetch the playlist
curl -s 'http://127.0.0.1:8756/hls/live/index.m3u8?ch=101'
# Grab the first segment and check it is H.264
seg=$(curl -s 'http://127.0.0.1:8756/hls/live/index.m3u8?ch=101' | grep -v '^#' | head -1)
curl -s -o /tmp/seg.ts "http://127.0.0.1:8756$seg"
ffprobe -v error -show_entries stream=codec_name -of csv /tmp/seg.ts
```

### Inspecting sessions and ffmpeg failures

Each session has a directory with its playlist, segments, and — critically —
**`ffmpeg-stderr.log`**. Almost every streaming bug is explained there (e.g. a
`401 Unauthorized` means the RTSP URL lost its credentials).

```bash
ls "$TMPDIR"/hikvision-viewer-hls-*        # or /var/folders/... on macOS
tail -5 .../live-101/ffmpeg-stderr.log
ps -eo pid,ppid,pcpu,etime,command | grep ffmpeg    # check for strays
```

### Simulating "no viewer"

Close the page (or hide the tab) and confirm ffmpeg exits:

```bash
curl -s 'http://127.0.0.1:8756/hls-live/index.m3u8?ch=101' >/dev/null
pgrep -f ffmpeg | wc -l        # >0 while the session is active
curl -s 'http://127.0.0.1:8756/hls-live-stop?ch=101'
sleep 2; pgrep -f ffmpeg | wc -l   # expect 0
```

### Browser checks

`hls.js` is vendored (`static/vendor/`). Verify in Firefox, Chrome, and Safari
(iOS) when touching the player: channel switching, the "tab hidden → paused"
state, and the measured-latency status line.

---

## Operational lessons (learned the hard way)

These caused real bugs and hours of confusion. Take them seriously.

1. **Restart the server after every code change, and verify by behaviour — not
   by `/healthz`.** A stale process can hold the port while a new one dies on
   "address already in use" (logged only in the server's stdout file). Check
   `lsof -nP -i :8756` when in doubt. A 401 on static assets or a 404 on a new
   route is almost always a stale server.
2. **Prefer the `edit` tool (exact match, loud failure) over scripted
   `str.replace` edits.** A scripted replace that does not match silently
   no-ops while printing success. If you must script it, re-read the file and
   assert afterwards.
3. **A missing `def` can coexist with a caller for a while** (e.g. a route
   calling `self.route_static(...)` with the method absent). `ruff` will not
   catch it; only hitting the route will. Exercise new routes once.
4. **Delete stale `__pycache__` if behaviour looks impossible** — same-second
   edits can defeat bytecode validation on some setups.
5. **Process hygiene**: never `kill -9` the server and walk away in testing —
   the watchdog handles it, but check `pgrep -f ffmpeg` afterwards anyway.
   Mock-mode test streams encode video continuously and will pin several cores
   while a page is open; close the page when done testing.
6. **`sendBeacon` is POST.** Any endpoint it targets must be reachable via
   POST as well as GET.

---

## Non-goals

- No cloud/Hik-Connect integration, no vendor SDK
- No JS framework or build step — plain HTML/CSS/JS served by the stdlib server
- No multi-camera management UI (one camera per instance; run more instances)
- No transcoding unless a source codec cannot be copied (see invariants)
