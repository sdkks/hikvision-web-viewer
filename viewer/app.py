"""HTTP server: pages, ISAPI proxying, and ffmpeg-backed HLS streaming."""

import base64
import hmac
import html
import json
import re
from datetime import datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import camera, config, coverage, motion, streams, templates, util

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_TYPES = {".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml"}

CHANNELS = ("101", "102")

# Download relay: stream the camera's file in 64 KiB chunks and never buffer it.
DOWNLOAD_CHUNK = 64 * 1024
# A camera file name is alnum/underscore/dot/dash; the query string is untrusted,
# so anything else is dropped rather than embedded in the ISAPI XML body.
_DOWNLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# Download containers we can name from the response's first bytes. The camera's
# own Content-Type is not trustworthy (this firmware sends "application/binary"),
# so the type and extension come from the bytes; when they match nothing we know,
# we say "opaque binary" rather than guess a video format.
_MPEG_PS_MAGIC = b"\x00\x00\x01\xba"  # MPEG program stream pack start code
_TS_PACKET_SIZE = 188
_TS_SYNC_BYTE = 0x47
_IMKH_MAGIC = b"IMKH"  # Hikvision's own wrapper around a recorded stream
_IMKH_SCAN = 4096


def sniff_download_container(first: bytes) -> tuple[str, str]:
    """Name the download's container from its first bytes -> (Content-Type, extension).

    MPEG program stream   -> video/mpeg / .mpg
    ISO base media (MP4)  -> video/mp4  / .mp4
    MPEG transport stream -> video/mp2t / .ts
    Anything else         -> application/octet-stream / .bin

    An unrecognised stream must not be given a video format we did not observe:
    the honest answer is "opaque binary", so the neutral type and a `.bin` suffix
    are used instead of inventing an extension. (A camera-declared video/* is not
    used here: the bytes are the only evidence, and this firmware's declared type
    is not even a video type.)

    This camera wraps the recording in a short `IMKH` header before the MPEG
    program stream: measured on a DS-2CV2121G2-IDW the pack start sits at offset
    40, and ffprobe reads the whole file as mpeg. Naming that `.mpg` describes the
    payload, where `.bin` would hide a perfectly playable video.
    """
    if first.startswith(_MPEG_PS_MAGIC):
        return "video/mpeg", "mpg"
    if first.startswith(_IMKH_MAGIC) and _MPEG_PS_MAGIC in first[:_IMKH_SCAN]:
        return "video/mpeg", "mpg"
    if first[4:8] == b"ftyp":
        return "video/mp4", "mp4"
    if (
        len(first) > _TS_PACKET_SIZE
        and first[0] == _TS_SYNC_BYTE
        and first[_TS_PACKET_SIZE] == _TS_SYNC_BYTE
    ):
        return "video/mp2t", "ts"
    return "application/octet-stream", "bin"


# A motion body is at most a 18x22 boolean array; 1 MB is already generous.
MAX_BODY_BYTES = 1_000_000
# How much of a rejected body we will read before answering (see _reject_body).
MAX_DRAIN_BYTES = 4_000_000
# ...and how long we will wait for it: a client that declares a huge body and
# never sends it must still get the typed reply, and must not pin the thread.
DRAIN_DEADLINE_S = 0.35

# A camera 401 is a config problem, not a broken camera. Say so wherever a
# camera failure is shown, and never collapse it into "did not answer".
CREDENTIALS_REFUSED = (
    "The camera refused the viewer's credentials. Fix the camera credentials "
    "(HIK_USER / HIK_PASSWORD, or the camera config file)."
)
CREDENTIALS_REFUSED_SHORT = "the camera refused the viewer's credentials"


def auth_required() -> bool:
    """Viewer-side protection is active only when both credentials are configured."""
    return bool(config.AUTH_USER and config.AUTH_PASSWORD)


def check_auth(header_value: str | None) -> bool:
    """Validate an HTTP Basic Authorization header (constant-time compare)."""
    if not auth_required():
        return True
    if not header_value or not header_value.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header_value.split(" ", 1)[1].strip(), validate=True)
        user, _, password = decoded.decode("utf-8").partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user, config.AUTH_USER) and hmac.compare_digest(
        password, config.AUTH_PASSWORD
    )


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


# Loopback names always acceptable: they name this machine, not the attacker.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Wildcard binds are not identities, so they never become an accepted Host.
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", "*", ""})


def _host_name(host_header: str) -> str:
    """Host-header value -> lowercased host name, port stripped, IPv6 un-bracketed."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # IPv6 literal: [::1]:8756 or [::1]
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if ":" in host else host


def allowed_hosts() -> set[str]:
    """Host names a request may claim: loopback, a specific bind host, and the
    operator's VIEWER_ALLOWED_HOSTS."""
    hosts = set(LOOPBACK_HOSTS)
    bind = _host_name(config.BIND_HOST)
    if bind not in _WILDCARD_BINDS:
        hosts.add(bind)
    hosts.update(_host_name(entry) for entry in config.ALLOWED_HOSTS)
    return {host for host in hosts if host}


def _is_json_content_type(value: str) -> bool:
    """True when the Content-Type media type is exactly application/json.

    Parsed as a media type so parameters (charset, boundary, ...) are allowed,
    but a substring such as application/jsonp or text/application/json is not.
    """
    return (value or "").split(";", 1)[0].strip().lower() == "application/json"


def motion_guard_reason(handler) -> str | None:
    """Why a POST /api/motion must be refused, or None when it may proceed.

    The viewer may run with HTTP Basic Auth, which browsers auto-attach to a
    cross-site form POST. Two cheap gates keep that honest without tokens or
    sessions: the Host must be one we recognise (a DNS-rebinding attacker
    controls the Origin *and* the Host, so Origin-vs-Host alone is not enough),
    and the Origin, when present, must agree with Host. A missing Origin is
    accepted: a same-host curl/CLI client is not a CSRF path. The body must be
    application/json.
    """
    host_header = handler.headers.get("Host", "")
    host = _host_name(host_header)
    if not host:
        return "the request had no Host header"
    if host not in allowed_hosts():
        return (
            f"host '{host}' is not an accepted host (a rebound Host is not accepted; "
            "a LAN or reverse-proxy host must be listed in VIEWER_ALLOWED_HOSTS)"
        )
    origin = handler.headers.get("Origin")
    if origin and urlparse(origin).netloc.lower() != host_header.lower():
        return "the Origin does not match the Host"
    if not _is_json_content_type(handler.headers.get("Content-Type", "")):
        return "the request body must be application/json"
    return None


def check_same_origin(handler) -> bool:
    """True only when motion_guard_reason finds no objection."""
    return motion_guard_reason(handler) is None


class ViewerApp:
    def __init__(self):
        self.cam = camera.make_camera()

    # ── page fragments ───────────────────────────────────────────────
    def footer_line(self) -> str:
        try:
            sd = self.cam.storage()
            line = (
                f"SD {util.human_size(sd['free_mb'] * 1024 * 1024)} free of "
                f"{util.human_size(sd['capacity_mb'] * 1024 * 1024)}"
            )
        except camera.CredentialsRejectedError:
            line = "SD storage: the camera refused the viewer's credentials"
        except camera.ServiceDownError:
            line = "SD storage: service not responding"
        except camera.UnreachableError:
            line = "SD storage: camera unreachable"
        except camera.BadResponseError:
            line = "SD storage: unexpected reply"
        except camera.CameraError:
            line = "SD storage: unavailable"
        try:
            info = self.cam.device_info()
            fw = f"{info.get('firmwareVersion', '')} {info.get('firmwareReleasedDate', '')}".strip()
            if fw:
                line += f" · fw {fw}"
        except camera.CameraError:
            pass
        return line

    def render(self, handler, title: str, content: str, nav: str, code: int = 200) -> None:
        active = {
            "nav_live": "page" if nav == "live" else "",
            "nav_rec": "page" if nav == "recordings" else "",
            "nav_motion": "page" if nav == "motion" else "",
        }
        page = templates.BASE.substitute(
            title=esc(title),
            brand=esc(config.CAM_HOST),
            content=content,
            footer=self.footer_line(),
            **active,
        )
        data = page.encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    # ── pages ────────────────────────────────────────────────────────
    def channel_label(self, channel: str, fallback: str) -> str:
        try:
            res = self.cam.stream_resolution(channel)
        except camera.CredentialsRejectedError:
            # Surface it (page_live shows the credentials banner) instead of
            # quietly falling back to a plausible-looking label.
            raise
        except camera.CameraError:
            res = None
        return f"{fallback} · {res[0]}×{res[1]}" if res else fallback

    def page_live(self, handler):
        channel = config.DEFAULT_CHANNEL if config.DEFAULT_CHANNEL in CHANNELS else "101"
        main_label, sub_label = "Main (full quality)", "Sub (lighter)"
        warning = ""
        try:
            main_label = self.channel_label("101", main_label)
            sub_label = self.channel_label("102", sub_label)
        except camera.CredentialsRejectedError:
            warning = templates.CREDENTIALS_WARNING
        content = templates.LIVE.replace("$camera_warning", warning)
        content = content.replace("$channel", channel)
        content = content.replace("$main_label", main_label)
        content = content.replace("$sub_label", sub_label)
        content = content.replace("$p_main", "true").replace("$p_sub", "false")
        # Codec hint: the mock encodes H.264; the real camera's main channel is H.265.
        content = content.replace("$codec_set", "avc" if config.MOCK else "hevc")
        self.render(handler, "Live", content, nav="live")

    def motion_ssr_summary(self) -> str:
        """Read-only summary for the no-JS floor; never blocks the JS editor."""
        try:
            doc = self.cam.motion_detection()
            cells = motion.decode_gridmap(doc["gridMap"])
        except camera.CredentialsRejectedError:
            return f"<p class='warnnote'>{esc(CREDENTIALS_REFUSED)}</p>"
        except camera.CameraError:
            return "<p class='muted'>The camera's motion configuration could not be read.</p>"
        except ValueError:
            return "<p class='muted'>The camera returned an unexpected zone format.</p>"
        state = "on" if doc["enabled"] else "off"
        enabled = (
            f"<p>Motion detection is <b>{state}</b>; sensitivity "
            f"{doc['sensitivityLevel']}; {esc(motion.summary(motion.counts(cells)))}.</p>"
        )
        return enabled

    def page_motion(self, handler):
        content = templates.MOTION.substitute(ssr_summary=self.motion_ssr_summary())
        self.render(handler, "Motion", content, nav="motion")

    def page_recordings(self, handler, query: dict):
        preset = (query.get("preset", [""])[0] or "").strip()
        frm_raw = (query.get("from", [""])[0] or "").strip()
        to_raw = (query.get("to", [""])[0] or "").strip()

        now = util.datetime_now()
        defaults = {
            "from": (now.replace(hour=0, minute=0, second=0, microsecond=0)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "to": now.strftime("%Y-%m-%dT%H:%M"),
        }
        from_val, to_val = frm_raw or defaults["from"], to_raw or defaults["to"]
        details_open = "open" if (frm_raw or to_raw) else ""

        if preset:
            windows = {
                "today": (None, True),
                "24h": (24, False),
                "3d": (72, False),
                "7d": (168, False),
            }
            if preset in windows:
                hours, midnight = windows[preset]
                frm_dt, to_dt = util.relative_preset(hours, midnight)
                from_val, to_val = (
                    frm_dt.strftime("%Y-%m-%dT%H:%M"),
                    to_dt.strftime("%Y-%m-%dT%H:%M"),
                )

        results = (
            "<p class='muted' id='idle-hint'>Choose a range to search, or pick a day "
            "below to browse what is available. Recordings are motion-triggered clips.</p>"
        )
        code = 200
        if "searched" in query or frm_raw or preset:
            results = self.run_search(from_val, to_val)
            if isinstance(results, tuple):
                results, code = results

        content = (
            templates.RECORDINGS.replace("$details_open", details_open)
            .replace("$from_val", esc(from_val))
            .replace("$to_val", esc(to_val))
            .replace("$results", results)
        )
        self.render(handler, "Recordings", content, nav="recordings", code=code)

    def run_search(self, from_val: str, to_val: str):
        """Search and render results. Returns html str, or (html, http_code) on failure."""
        try:
            frm_dt, to_dt = util.parse_user_range(from_val, to_val)
        except ValueError as exc:
            card = (
                f"<div class='errcard'><p>{esc(exc)}</p>"
                f"<p class='muted'>Range is kept — adjust and search again.</p></div>"
            )
            return card, 400

        try:
            clips = self.cam.search(util.iso_search_time(frm_dt), util.iso_search_time(to_dt))
        except camera.UnreachableError as exc:
            card = (
                f"<div class='errcard'><p>Camera unreachable at {esc(config.CAM_HOST)}.</p>"
                f"<p class='muted'>{esc(exc)}</p></div>"
            )
            return card, 502
        except camera.CredentialsRejectedError:
            card = (
                f"<div class='errcard'><p>{esc(CREDENTIALS_REFUSED)}</p>"
                f"<p class='muted'>Range is kept — fix the credentials and search again.</p></div>"
            )
            return card, 502
        except camera.ServiceDownError:
            card = (
                "<div class='errcard'><p>The camera's search service isn't responding "
                "(this camera's search is known to be flaky). Live view and clip playback "
                "still work — try again in a moment.</p>"
                "<a class='retry' style='display:inline-flex;align-items:center;"
                "text-decoration:none' "
                f"href='/recordings?from={esc(from_val)}&amp;to={esc(to_val)}&amp;searched=1'>"
                "Retry</a></div>"
            )
            return card, 503
        except camera.BadResponseError:
            card = (
                "<div class='errcard'><p>The camera sent an unexpected search reply. "
                "Live view still works.</p></div>"
            )
            return card, 502

        if not clips:
            return "<p>No recordings in this range.</p>"

        overflow = ""
        if len(clips) >= 100:
            overflow = "<p class='muted'>Showing first 100 — narrow the range to see more.</p>"

        items = []
        for clip in clips[:100]:
            start_local = util.local_display(clip.start_dt)
            end_local = util.local_display(clip.end_dt)
            dur_h = util.human_duration(clip.duration_s)
            size_h = util.human_size(clip.size) if clip.size else ""
            meta = f"{dur_h} &middot; {size_h}" if size_h else dur_h
            from_cam = util.to_cam_time(clip.start_dt)
            to_cam = util.to_cam_time(clip.end_dt)
            play_href = (
                f"/play?from={esc(from_cam)}&amp;to={esc(to_cam)}&amp;dur={clip.duration_s}"
                f"&amp;size={clip.size}"
            )
            ffmpeg_cmd = (
                f'ffmpeg -rtsp_transport tcp -i "{clip.uri}" -c copy "clip_{clip.start_utc}.mp4"'
            )
            items.append(
                f"<li class='clip'><div class='times'>{esc(start_local)} &rarr; "
                f"{esc(end_local)}</div>"
                f"<div class='meta'>{meta}</div>"
                f"<div class='clipactions'>"
                f"<a class='playbtn' href='{play_href}'>&#9654; Play</a>"
                f"<a class='dlbtn' href='/download?from={esc(from_cam)}&amp;to={esc(to_cam)}"
                f"&amp;name={esc(clip.name)}&amp;size={clip.size}'>&#8681; Download</a></div>"
                f"<details><summary>rtsp URI &middot; ffmpeg</summary>"
                f"<pre id='u{clip.start_utc}'>{esc(clip.uri)}</pre>"
                f"<button type='button' class='copybtn' data-target='#u{clip.start_utc}'>Copy URI</button>"
                f"<pre id='f{clip.start_utc}'>{esc(ffmpeg_cmd)}</pre>"
                f"<button type='button' class='copybtn' data-target='#f{clip.start_utc}'>Copy ffmpeg</button>"
                f"</details></li>"
            )

        n = len(clips)
        noun = "clip" if n == 1 else "clips"
        range_label = f"{util.local_display(frm_dt)} and {util.local_display(to_dt)}"
        return (
            f"<p><b>{n}</b> {noun} between {esc(range_label)}</p>{overflow}"
            f"<ul class='clips'>{''.join(items)}</ul>"
            "<p class='muted'>Times shown in the server's timezone; the camera stores UTC.</p>"
        )

    def page_play(self, handler, query: dict):
        from_cam = (query.get("from", [""])[0] or "").strip()
        to_cam = (query.get("to", [""])[0] or "").strip()
        try:
            start_dt = util.parse_cam_time(from_cam)
            end_dt = util.parse_cam_time(to_cam)
            dur_s = max(1, int((end_dt - start_dt).total_seconds()))
        except ValueError:
            self.render(
                handler,
                "Clip",
                "<div class='errcard'><p>Bad clip parameters.</p>"
                "<a class='retry' href='/recordings' "
                "style='display:inline-flex;align-items:center;"
                "text-decoration:none'>&larr; Results</a></div>",
                nav="recordings",
                code=400,
            )
            return
        # Clip duration/size are passed to the client for the restart bar; the
        # position readout is derived from wall-clock in JS, not from video time.
        size_raw = (query.get("size", [""])[0] or "").strip()
        size = int(size_raw) if size_raw.isdigit() else 0
        size_part = f" &middot; {util.human_size(size)}" if size else ""
        # The restart bar spans the original clip window, not just where this
        # session started: a restart into the middle of a clip must not remove
        # the earlier part from the control. `win` carries that window start
        # across restarts; without it (a first arrival from the picker) the
        # window is simply the session's own [from, to].
        from_epoch = int(start_dt.timestamp())
        win_raw = (query.get("win", [""])[0] or "").strip()
        window_from_dt = start_dt
        if win_raw:
            try:
                window_from_dt = util.parse_cam_time(win_raw)
            except ValueError:
                window_from_dt = start_dt
        # The window can never begin after the session it contains.
        if window_from_dt > start_dt:
            window_from_dt = start_dt
        window_from_epoch = int(window_from_dt.timestamp())
        # Download the clip's own window as the restart bar spans it: the original
        # window start through the session end, never a shortened session start, so
        # a download after a restart still names the clip it came from. The camera
        # ignores the requested window and sends the whole stored file (see
        # route_download), so the page must not promise a trimmed export.
        size_q = f"&amp;size={size}" if size_raw.isdigit() else ""
        download_href = (
            f"/download?from={util.to_cam_time(window_from_dt)}"
            f"&amp;to={util.to_cam_time(end_dt)}{size_q}"
        )
        local_start = start_dt.astimezone()
        results_href = (
            f"/recordings?date={local_start.strftime('%Y-%m-%d')}&hour={local_start.hour}"
        )
        content = templates.PLAY.substitute(
            results_href=esc(results_href),
            start_local=esc(util.local_display(start_dt)),
            end_local=esc(util.local_display(end_dt)),
            dur_h=util.human_duration(dur_s),
            size_part=size_part,
            dur_s=dur_s,
            from_cam=esc(from_cam),
            to_cam=esc(to_cam),
            download_href=download_href,
            from_epoch=from_epoch,
            window_from_epoch=window_from_epoch,
            to_epoch=int(end_dt.timestamp()),
            size_raw=size,
        )
        self.render(handler, "Playback", content, nav="recordings")

    # ── streams & data ───────────────────────────────────────────────
    def route_static(self, handler, name: str):
        if not name:
            handler.send_error(404)
            return
        file_path = (STATIC_DIR / name).resolve()
        if not str(file_path).startswith(str(STATIC_DIR)) or not file_path.is_file():
            handler.send_error(404)
            return
        data = file_path.read_bytes()
        handler.send_response(200)
        handler.send_header(
            "Content-Type", STATIC_TYPES.get(file_path.suffix, "application/octet-stream")
        )
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        handler.wfile.write(data)

    def route_hls_playlist(self, handler, kind: str, query: dict):
        """Ensure the session exists and serve its playlist with absolute segment URLs.

        Seeking is session-restart semantics: requesting the playlist with an adjusted
        `from` (inside the clip window) restarts the camera playback at that point.
        The camera also honors RTSP Range: npt/clock and Scale: 1-4 (see ISAPI spec
        ch. RTSP playback), which we approximate this way without raw RTSP control.
        """
        if kind == "live":
            channel = query.get("ch", ["101"])[0] or "101"
            if channel not in CHANNELS:
                handler.send_error(404)
                return
            transcode = streams.needs_transcode(self.cam.stream_codec(channel))
            session = streams.ensure_live(channel, transcode=transcode)
        elif kind == "play":
            from_cam = (query.get("from", [""])[0] or "").strip()
            to_cam = (query.get("to", [""])[0] or "").strip()
            if not (util._CAM_RE.match(from_cam) and util._CAM_RE.match(to_cam)):
                handler.send_error(400, "bad clip window")
                return
            transcode = streams.needs_transcode(self.cam.stream_codec("101"))
            session = streams.ensure_play(from_cam, to_cam, transcode=transcode)
        else:
            handler.send_error(404)
            return
        key = session["key"]

        # The camera allows ONE playback session; a stale one server-side keeps the
        # slot busy for a while after an abrupt client death. Wait for the first
        # playlist; if ffmpeg dies fast (camera busy), retry the session a few times.
        playlist_path = session["dir"] / "index.m3u8"
        import time as _time

        attempts = 0
        deadline = _time.monotonic() + 12.0
        while not playlist_path.is_file() and _time.monotonic() < deadline:
            _time.sleep(0.25)
            if playlist_path.is_file():
                break
            if session["proc"].poll() is not None:
                attempts += 1
                if attempts > 3:
                    break
                # camera busy or transient failure: respawn (single-slot enforced)
                if session["kind"] == "play":
                    session = streams.ensure_play(from_cam, to_cam, transcode=transcode)
                else:
                    channel = query.get("ch", ["101"])[0] or "101"
                    session = streams.ensure_live(channel, transcode=transcode)
        if not playlist_path.is_file():
            handler.send_error(
                503,
                "stream not starting - the camera allows one playback session and may "
                "still be holding it; wait ~30 s and retry",
            )
            return
        text = playlist_path.read_text()
        base = f"/hls/media/{key}/"
        lines = [
            line if line.startswith("#") else base + line.strip()
            for line in text.splitlines()
            if line.strip()
        ]
        body = ("\n".join(lines) + "\n").encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/vnd.apple.mpegurl")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def route_hls_media(self, handler, key: str, name: str):
        if "/" in name or ".." in name or not name.endswith((".ts", ".m3u8")):
            handler.send_error(404)
            return
        directory = streams.session_dir(key)
        file_path = directory / name if directory else None
        if not file_path or not file_path.is_file():
            handler.send_error(404)
            return
        data = file_path.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", "video/mp2t")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(data)

    def route_hls_stop(self, handler, path: str, query: dict):
        """Explicit stop: the viewer says nobody is watching (tab hidden/closed)."""
        if path == "/hls-live-stop":
            channel = query.get("ch", ["101"])[0] or "101"
            stopped = streams.stop_key("live", channel)
        else:
            stopped = streams.stop_play()
        body = b'{"stopped": ' + (b"true" if stopped else b"false") + b"}"
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def route_hls(self, handler, path: str, query: dict):
        # /hls/<key>/index.m3u8  |  /hls/media/<key>/<segment>
        parts = [p for p in path.split("/") if p][1:]  # drop 'hls'
        if len(parts) == 2 and parts[1] == "index.m3u8":
            return self.route_hls_playlist(handler, parts[0], query)
        if len(parts) == 3 and parts[0] == "media":
            return self.route_hls_media(handler, parts[1], parts[2])
        handler.send_error(404)

    def route_coverage(self, handler, query: dict):
        """Day coverage map for the picker: hours/minutes levels + merged ranges.

        One camera search per request; the client caches per date. States mirror
        the honesty contract so "search down" never looks like "no recordings".
        """
        raw_date = (query.get("date", [""])[0] or "").strip()
        try:
            day = datetime.fromisoformat(raw_date).date()
        except ValueError:
            self.send_json(
                handler, {"state": "bad_request", "state_detail": "date=YYYY-MM-DD"}, 400
            )
            return
        day_start = datetime.combine(day, time.min).astimezone()
        day_end = day_start + timedelta(days=1)
        payload = {
            "date": day.isoformat(),
            "tz": str(datetime.now().astimezone().tzname()),
            "state": "ok",
            "state_detail": "",
            "hours": [0] * 24,
            "hour_clips": [0] * 24,
            "minutes": [[0] * 60 for _ in range(24)],
            "ranges": [],
            "files": [],
        }
        try:
            clips = self.cam.search(
                util.iso_search_time(day_start), util.iso_search_time(day_end), max_results=200
            )
        except camera.CredentialsRejectedError:
            payload.update(state="credentials", state_detail=CREDENTIALS_REFUSED_SHORT)
        except camera.ServiceDownError:
            payload.update(
                state="service_down", state_detail="the camera did not answer the search request"
            )
        except camera.UnreachableError:
            payload.update(state="unreachable", state_detail=f"no route to {config.CAM_HOST}")
        except camera.BadResponseError:
            payload.update(state="bad_response", state_detail="reply was not the expected XML")
        else:
            if len(clips) >= 200:
                payload["state"] = "truncated"
                payload["state_detail"] = (
                    "more clips than one query returns; coverage may be partial"
                )
            built = coverage.build_coverage(clips, day_start)
            payload.update(built)
            if payload["state"] == "ok" and not clips:
                payload["state"] = "empty"
        self.send_json(handler, payload)

    def route_neighbours(self, handler, query: dict):
        """Recordings before/after the clip being watched (nearest first)."""
        from_cam = (query.get("from", [""])[0] or "").strip()
        to_cam = (query.get("to", [""])[0] or "").strip()
        if not (util._CAM_RE.match(from_cam) and util._CAM_RE.match(to_cam)):
            self.send_json(handler, {"state": "bad_request"}, 400)
            return
        start = util.parse_cam_time(from_cam)
        end = util.parse_cam_time(to_cam)
        # The restart bar's coverage window can be wider than this session's
        # start after any number of restarts; `win` names that original start.
        # Neighbours stay relative to the current session start (`start`), while
        # only the window eligibility follows the wider window.
        window_start = start
        win_cam = (query.get("win", [""])[0] or "").strip()
        if win_cam:
            try:
                window_start = util.parse_cam_time(win_cam)
            except ValueError:
                window_start = start
        # A window that begins after the session it contains is meaningless.
        if window_start > start:
            window_start = start
        day_start = start.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        payload = {
            "state": "ok",
            "state_detail": "",
            "earlier": [],
            "later": [],
            "more_earlier": False,
            "more_later": False,
            "truncated": False,
        }
        try:
            clips = self.cam.search(
                util.iso_search_time(day_start),
                util.iso_search_time(day_start + timedelta(days=1)),
                max_results=200,
            )
        except camera.CredentialsRejectedError:
            payload.update(state="credentials", state_detail=CREDENTIALS_REFUSED_SHORT)
        except camera.ServiceDownError:
            payload.update(
                state="service_down", state_detail="the camera did not answer the search request"
            )
        except camera.UnreachableError:
            payload.update(state="unreachable", state_detail=f"no route to {config.CAM_HOST}")
        except camera.BadResponseError:
            payload.update(state="bad_response", state_detail="reply was not the expected XML")
        else:
            built = coverage.build_coverage(clips, day_start)
            payload.update(
                coverage.neighbours(built["files"], int(start.timestamp()), int(end.timestamp()))
            )
            if len(clips) >= 200:
                # Mirror route_coverage: a truncated search may be missing clips,
                # so its window is not trustworthy as "covered vs gap" and must
                # never paint a real-looking "no recording". Omit the window; the
                # bar then falls back to its "coverage couldn't be checked" state.
                payload["truncated"] = True
            else:
                # The restart bar's eligible sub-intervals for this exact clip
                # window, clipped from the same day search this handler already
                # ran so the play page never triggers a second camera search.
                payload["window"] = coverage.window_eligibility(
                    built["files"],
                    int(window_start.timestamp()),
                    int(end.timestamp()),
                    target_epoch=int(start.timestamp()),
                )
        self.send_json(handler, payload)

    # ── motion detection ────────────────────────────────────────────
    def _motion_view(self, enabled, enable_highlight, sensitivity, cells) -> dict:
        counted = motion.counts(cells)
        return {
            "enabled": bool(enabled),
            "enable_highlight": bool(enable_highlight),
            "sensitivity": int(sensitivity),
            "cells": cells,
            "counts": counted,
            "summary": motion.summary(counted),
        }

    def _motion_view_from_doc(self, doc: dict) -> dict:
        cells = motion.decode_gridmap(doc["gridMap"])
        return self._motion_view(
            doc["enabled"], doc["enableHighlight"], doc["sensitivityLevel"], cells
        )

    def _motion_intent(self, body) -> dict:
        """Validate an untrusted POST body into an intended state (raises ValueError)."""
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        cells = body.get("cells")
        if not (isinstance(cells, list) and len(cells) == motion.ROWS):
            raise ValueError(f"cells must be a {motion.ROWS}x{motion.COLS} boolean array")
        grid: list[list[bool]] = []
        for row in cells:
            # Real booleans only: bool("0") is True, so coercing would arm a cell
            # the caller asked to leave masked while claiming a boolean array.
            if (
                not isinstance(row, list)
                or len(row) != motion.COLS
                or not all(isinstance(cell, bool) for cell in row)
            ):
                raise ValueError(f"cells must be a {motion.ROWS}x{motion.COLS} boolean array")
            grid.append(list(row))
        sensitivity = body.get("sensitivity")
        if not motion.valid_sensitivity(sensitivity):
            raise ValueError(f"sensitivity must be one of {list(motion.SENSITIVITY_STEPS)}")
        return {
            "enabled": bool(body.get("enabled")),
            "enable_highlight": bool(body.get("enable_highlight")),
            "sensitivity": sensitivity,
            "cells": grid,
            "gridMap": motion.encode_gridmap(grid),
        }

    def _motion_resolution(self):
        try:
            return self.cam.stream_resolution("101")
        except camera.CameraError:
            return None

    def route_motion_get(self, handler, query=None):
        payload: dict[str, object] = {"state": "ok", "state_detail": ""}
        try:
            doc = self.cam.motion_detection()
        except camera.CredentialsRejectedError:
            payload.update(state="credentials", state_detail=CREDENTIALS_REFUSED_SHORT)
        except camera.UnreachableError:
            payload.update(state="unreachable", state_detail=f"no route to {config.CAM_HOST}")
        except camera.ServiceDownError:
            payload.update(
                state="service_down", state_detail="the camera did not answer the motion request"
            )
        except camera.BadResponseError as exc:
            payload.update(state="bad_response", state_detail=str(exc))
        except camera.CameraError:
            payload.update(
                state="bad_response", state_detail="the camera's motion reply was not usable"
            )
        else:
            payload.update(self._motion_view_from_doc(doc))
            payload["valid_sensitivities"] = list(motion.SENSITIVITY_STEPS)
            payload["region_type"] = doc.get("regionType", "")
            payload["row_granularity"] = doc.get("rowGranularity", "")
            payload["column_granularity"] = doc.get("columnGranularity", "")
            payload["resolution"] = self._motion_resolution()
        self.send_json(handler, payload)

    def route_motion_post(self, handler):
        reason = motion_guard_reason(handler)
        if reason is not None:
            self.send_json(handler, {"state": "forbidden", "state_detail": reason}, 403)
            return
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except TypeError, ValueError:
            self._reject_body(handler, 0, "Content-Length is not a number")
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reject_body(handler, length, "missing or oversized body")
            return
        raw = handler.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
            intended = self._motion_intent(body)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            self.send_json(handler, {"state": "bad_request", "state_detail": str(exc)}, 400)
            return
        self.send_json(handler, self.motion_write(intended))

    def _reject_body(self, handler, length: int, detail: str) -> None:
        """Answer a bad body with the typed 400 after draining what already arrived.

        Answering before the client has finished writing leaves unread bytes in
        the socket and a close then resets the connection, so the typed reply can
        be lost. So we drain -- but only up to a cap and only for a short deadline:
        a client that declares a huge body and never sends it must still receive
        the reply (and must not hold this thread open), while a client that really
        is sending one gets its socket drained so the answer survives the close.
        """
        handler.close_connection = True
        remaining = max(0, min(length, MAX_DRAIN_BYTES))
        sock = getattr(handler, "connection", None)
        previous = sock.gettimeout() if sock is not None else None
        if remaining and sock is not None:
            sock.settimeout(DRAIN_DEADLINE_S)
        try:
            while remaining > 0:
                chunk = handler.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            if sock is not None:
                sock.settimeout(previous)
        self.send_json(handler, {"state": "bad_request", "state_detail": detail}, 400)

    def motion_write(self, intended: dict) -> dict:
        """GET fresh -> validate base -> merge -> PUT -> re-GET. The readback is the answer.

        `write_attempted` marks a PUT that may or may not have landed (a timeout or
        dropped connection). When it is set the reply carries whatever the camera
        currently reads so the UI can say "may not have been saved" instead of
        asserting an unobserved "nothing was saved".
        """
        intended_view = self._motion_view(
            intended["enabled"],
            intended["enable_highlight"],
            intended["sensitivity"],
            intended["cells"],
        )
        try:
            base_xml = self.cam.motion_detection_xml()
        except camera.CredentialsRejectedError:
            return {
                "state": "credentials",
                "state_detail": CREDENTIALS_REFUSED_SHORT,
                "write_attempted": False,
            }
        except camera.UnreachableError:
            return {
                "state": "unreachable",
                "state_detail": f"no route to {config.CAM_HOST}",
                "write_attempted": False,
            }
        except camera.ServiceDownError:
            return {
                "state": "service_down",
                "state_detail": "the camera did not answer the motion request",
                "write_attempted": False,
            }
        except camera.BadResponseError as exc:
            return {"state": "bad_response", "state_detail": str(exc), "write_attempted": False}

        # Never merge into a document that is not a motion document we can edit:
        # a 200 text/html login page would otherwise be rewritten and PUT back.
        try:
            base_doc = camera.parse_motion_detection(base_xml)
        except camera.BadResponseError as exc:
            return {
                "state": "bad_response",
                "write_attempted": False,
                "state_detail": (
                    "refusing to write: the camera's current reply is not a motion "
                    f"document with an editable zone map ({exc})"
                ),
            }
        # Preserve the camera's per-row padding bits from the document we read.
        grid_map = motion.encode_gridmap(intended["cells"], base_grid_map=base_doc["gridMap"])

        try:
            merged = camera.apply_motion_fields(
                base_xml,
                enabled=intended["enabled"],
                enable_highlight=intended["enable_highlight"],
                sensitivity=intended["sensitivity"],
                grid_map=grid_map,
            )
        except camera.BadResponseError as exc:
            return {"state": "bad_response", "state_detail": str(exc), "write_attempted": False}

        try:
            self.cam.put_motion_detection(merged)
        except camera.CredentialsRejectedError:
            # 401 means the request was refused before it executed: nothing landed.
            return {
                "state": "credentials",
                "state_detail": CREDENTIALS_REFUSED_SHORT,
                "write_attempted": False,
            }
        except camera.RejectedError as exc:
            return {"state": "rejected", "state_detail": str(exc), "write_attempted": False}
        except camera.UnreachableError:
            # The request may have gone out before the connection dropped.
            return {
                "state": "unreachable",
                "state_detail": f"no route to {config.CAM_HOST}",
                "write_attempted": True,
                "actual": self._motion_readback_safe(),
            }
        except camera.BadResponseError as exc:
            # The camera answered 400/415: it understood and refused, so nothing landed.
            return {"state": "bad_response", "state_detail": str(exc), "write_attempted": False}
        except camera.ServiceDownError:
            # The write may have landed despite a timeout; read back before claiming failure.
            actual_view = self._motion_readback_safe()
            if actual_view is not None and not motion.diff(intended_view, actual_view):
                return self._motion_ok(intended_view, actual_view)
            return {
                "state": "service_down",
                "state_detail": "the camera did not answer the write",
                "write_attempted": True,
                "actual": actual_view,
            }
        actual_view = self._motion_readback_safe()
        if actual_view is None:
            return {
                "state": "bad_response",
                "write_attempted": True,
                "state_detail": "wrote the change but could not read it back",
            }
        return self._motion_ok(intended_view, actual_view)

    def _motion_readback_safe(self):
        try:
            return self._motion_view_from_doc(self.cam.motion_detection())
        except camera.CameraError, ValueError:
            return None

    def _motion_ok(self, intended_view, actual_view) -> dict:
        differences = motion.diff(intended_view, actual_view)
        return {
            "state": "ok",
            "match": not differences,
            "differences": differences,
            "intended": intended_view,
            "actual": actual_view,
            "state_detail": "",
        }

    def send_json(self, handler, payload: dict, code: int = 200):
        body = json.dumps(payload).encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    def route_download(self, handler, query: dict):
        """Relay a recorded file from the camera's ISAPI download endpoint.

        The picker link carries the clip window plus the camera's own file name
        and size, so the playback URI is rebuilt here from those fields — the
        same shape /play constructs for RTSP (viewer/streams.py) and the same
        shape as Clip.uri_escaped. A camera search per download is deliberately
        avoided: it adds latency and depends on the search service this firmware
        is known to drop. The camera ignores a narrowed window and streams the
        whole stored file containing it, so neither the page nor the filename
        promises a trimmed export — the filename names the window that was asked
        for, not a video that was cut to it.
        """
        from_cam = (query.get("from", [""])[0] or "").strip()
        to_cam = (query.get("to", [""])[0] or "").strip()
        if not (util._CAM_RE.match(from_cam) and util._CAM_RE.match(to_cam)):
            self.render(
                handler,
                "Download",
                "<div class='errcard'><p>Bad clip parameters.</p>"
                "<a class='retry' href='/recordings' style='display:inline-flex;"
                "align-items:center;text-decoration:none'>&larr; Results</a></div>",
                nav="recordings",
                code=400,
            )
            return

        name = (query.get("name", [""])[0] or "").strip()
        size_raw = (query.get("size", [""])[0] or "").strip()
        safe_name = name if _DOWNLOAD_NAME_RE.match(name) else ""

        uri_escaped = (
            f"{config.RTSP_SCHEME}://{config.CAM_HOST}:{config.RTSP_PORT}/Streaming/tracks/101/"
            f"?starttime={from_cam}&amp;endtime={to_cam}"
        )
        if safe_name:
            uri_escaped += f"&amp;name={html.escape(safe_name, quote=False)}"
        if size_raw.isdigit():
            uri_escaped += f"&amp;size={size_raw}"

        retry_q = f"from={esc(from_cam)}&amp;to={esc(to_cam)}"
        if safe_name:
            retry_q += f"&amp;name={esc(safe_name)}"
        if size_raw.isdigit():
            retry_q += f"&amp;size={size_raw}"
        retry_href = f"/download?{retry_q}"

        try:
            response = (
                streams.mock_download_response()
                if config.MOCK
                else self.cam.download_stream(uri_escaped)
            )
        except camera.CredentialsRejectedError:
            self._download_error(
                handler,
                CREDENTIALS_REFUSED,
                "Fix the credentials and try the download again.",
                code=502,
            )
            return
        except camera.UnreachableError as exc:
            self._download_error(
                handler,
                f"Camera unreachable at {config.CAM_HOST}.",
                str(exc),
                retry_href=retry_href,
                code=502,
            )
            return
        except camera.CameraError:
            self._download_error(
                handler,
                "The camera's download service isn't responding.",
                "Live view and playback may still work — try the download again in a moment.",
                retry_href=retry_href,
                code=503,
            )
            return

        # Sniff the container's first bytes before any header goes out: the bytes
        # decide both the Content-Type and the extension. The first chunk is held
        # back and written after the headers, then the rest of the stream is
        # relayed unchanged -- the file is never buffered whole.
        #
        # A reply that yields no bytes at all (the mock encoder failing, or a real
        # camera answering 200 with an empty file) is a bad reply, not a good
        # empty download: catch it here, before any status is committed, and
        # answer a typed error card instead of a successful-looking 0-byte file.
        try:
            try:
                stream = response.iter_content(DOWNLOAD_CHUNK)
                first = next((chunk for chunk in stream if chunk), b"")
            except camera.BadResponseError as exc:
                self._download_error(
                    handler,
                    "The camera returned no data for that range.",
                    str(exc),
                    retry_href=retry_href,
                    code=502,
                )
                return
            except BrokenPipeError, ConnectionResetError, TimeoutError:
                return  # the browser cancelled or vanished mid-download; nothing to report

            if not first:
                self._download_error(
                    handler,
                    "The camera returned no data for that range.",
                    "The download service answered but sent an empty file.",
                    retry_href=retry_href,
                    code=502,
                )
                return

            content_type, extension = sniff_download_container(first)
            filename = f"clip_{from_cam}_{to_cam}.{extension}"
            try:
                handler.send_response(200)
                handler.send_header("Content-Type", content_type)
                handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Connection", "close")
                handler.end_headers()
                handler.wfile.write(first)
                for chunk in stream:
                    if chunk:
                        handler.wfile.write(chunk)
            except BrokenPipeError, ConnectionResetError, TimeoutError:
                pass  # the browser cancelled or vanished mid-download; nothing to report
        finally:
            try:
                response.close()
            except Exception:
                pass

    def _download_error(
        self, handler, message: str, detail: str, retry_href: str = "", code: int = 502
    ) -> None:
        """A download failure is still a browser navigation: answer with a card."""
        retry = ""
        if retry_href:
            retry = (
                "<a class='retry' style='display:inline-flex;align-items:center;"
                "text-decoration:none' "
                f"href='{esc(retry_href)}'>Retry</a>"
            )
        self.render(
            handler,
            "Download",
            f"<div class='errcard'><p>{esc(message)}</p>"
            f"<p class='muted'>{esc(detail)}</p>{retry}</div>",
            nav="recordings",
            code=code,
        )

    def route_snapshot(self, handler, query: dict):
        channel = query.get("ch", ["101"])[0] or "101"
        if channel not in CHANNELS:
            handler.send_error(404)
            return
        try:
            if config.MOCK:
                jpeg = streams.mock_snapshot_jpeg()
            else:
                jpeg = self.cam.snapshot_jpeg(channel)
        except camera.CameraError as exc:
            handler.send_error(502, f"snapshot failed: {exc}")
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(len(jpeg)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(jpeg)

    def route_isapi(self, handler, path: str):
        try:
            text = self.cam._get("/ISAPI" + path)
        except camera.UnreachableError as exc:
            self.render(
                handler,
                "ISAPI",
                f"<div class='errcard'><p>{esc(exc)}</p></div>",
                nav="live",
                code=502,
            )
            return
        except camera.CameraError as exc:
            self.render(
                handler,
                "ISAPI",
                f"<div class='errcard'><p>{esc(exc)}</p></div>",
                nav="live",
                code=502,
            )
            return
        self.render(
            handler,
            f"ISAPI {esc(path)}",
            f"<h2>GET {esc(path)}</h2><pre>{esc(text[:20000])}</pre>",
            nav="live",
        )

    # ── dispatch ─────────────────────────────────────────────────────
    def dispatch(self, handler):
        if not check_auth(handler.headers.get("Authorization")):
            body = b"Authentication required.\n"
            handler.send_response(401)
            handler.send_header(
                "WWW-Authenticate", 'Basic realm="hikvision-web-viewer", charset="UTF-8"'
            )
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        parsed = urlparse(handler.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        if path == "/":
            return self.page_live(handler)
        if path == "/recordings":
            return self.page_recordings(handler, query)
        if path == "/play":
            return self.page_play(handler, query)
        if path == "/motion":
            return self.page_motion(handler)
        if path == "/api/motion":
            if handler.command == "POST":
                return self.route_motion_post(handler)
            return self.route_motion_get(handler, query)
        if path.startswith("/hls/"):
            return self.route_hls(handler, path, query)
        if path in ("/hls-live-stop", "/hls-play-stop"):
            return self.route_hls_stop(handler, path, query)
        if path == "/api/coverage":
            return self.route_coverage(handler, query)
        if path == "/api/neighbours":
            return self.route_neighbours(handler, query)
        if path == "/download":
            return self.route_download(handler, query)
        if path == "/snapshot":
            return self.route_snapshot(handler, query)
        if path.startswith("/isapi/"):
            return self.route_isapi(handler, path[len("/isapi") :])
        if path.startswith("/static/"):
            return self.route_static(handler, path[len("/static/") :])
        if path == "/healthz":
            body = b"ok"
            handler.send_response(200)
            handler.send_header("Content-Type", "text/plain")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        handler.send_error(404)


def make_handler(app: ViewerApp):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet unless things break
            pass

        def do_GET(self):
            app.dispatch(self)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/hls-live-stop", "/hls-play-stop", "/api/motion"):
                app.dispatch(self)
            else:
                handler = self
                handler.send_response(405)
                handler.send_header("Allow", "GET, POST")
                handler.end_headers()

    return Handler


def run() -> None:
    import atexit
    import signal
    import sys

    # A broken VIEWER_CONFIG stops startup with its own message, before anything
    # binds a port or builds a camera. Importing config never raises, so this is
    # the one place the error is turned into a non-zero exit.
    if config.CONFIG_ERROR:
        print(f"hikvision-web-viewer: {config.CONFIG_ERROR}", file=sys.stderr)
        raise SystemExit(2)
    for note in config.CONFIG_NOTES:
        print(f"hikvision-web-viewer: {note}", file=sys.stderr)

    app = ViewerApp()

    def _cleanup() -> None:
        streams.kill_all()

    def _on_sigterm(*_args) -> None:
        streams.kill_all()
        sys.exit(0)

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _on_sigterm)

    server = ThreadingHTTPServer((config.BIND_HOST, config.BIND_PORT), make_handler(app))
    server.daemon_threads = True
    mode = "MOCK" if config.MOCK else f"camera {config.CAM_HOST}"
    print(f"hikvision-web-viewer on http://{config.BIND_HOST}:{config.BIND_PORT} ({mode})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
