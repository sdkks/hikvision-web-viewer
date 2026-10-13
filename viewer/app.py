"""HTTP server: pages, ISAPI proxying, and ffmpeg-backed fMP4 streaming."""

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import camera, config, streams, templates, util

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_TYPES = {".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml"}

CHANNELS = ("101", "102")


def esc(value: str) -> str:
    return html.escape(str(value), quote=True)


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
        except camera.CameraError:
            line = "SD storage: unavailable (camera app layer down)"
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
    def page_live(self, handler):
        content = templates.LIVE.replace("$channel", "101")
        content = content.replace("$p_main", "true").replace("$p_sub", "false")
        # Codec hint: the mock encodes H.264; the real camera's main channel is H.265.
        content = content.replace("$codec_set", "avc" if config.MOCK else "hevc")
        self.render(handler, "Live", content, nav="live")

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
            "<p class='muted'>Choose a range to search. Recordings are motion-triggered clips.</p>"
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
                "<div class='errcard'><p>The camera gave an unexpected search reply "
                "(possible firmware drift). Live view still works.</p></div>"
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
        # Match the JS counter format: h:mm:ss once over an hour, m:ss below.
        m, secs = divmod(dur_s, 60)
        h, m = divmod(m, 60)
        dur_mmss = f"{h}:{m:02d}:{secs:02d}" if h else f"{m}:{secs:02d}"
        size_raw = (query.get("size", [""])[0] or "").strip()
        size = int(size_raw) if size_raw.isdigit() else 0
        size_part = f" &middot; {util.human_size(size)}" if size else ""
        content = templates.PLAY.substitute(
            start_local=esc(util.local_display(start_dt)),
            end_local=esc(util.local_display(end_dt)),
            dur_h=util.human_duration(dur_s),
            size_part=size_part,
            dur_s=dur_s,
            from_cam=esc(from_cam),
            to_cam=esc(to_cam),
            dur_mmss=dur_mmss,
        )
        self.render(handler, "Playback", content, nav="recordings")

    # ── streams & data ───────────────────────────────────────────────
    def stream_response(self, handler, proc):
        """Pipe ffmpeg stdout to the client as a close-delimited fMP4 stream."""
        handler.send_response(200)
        handler.send_header("Content-Type", "video/mp4")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()

        def write_chunk(chunk: bytes) -> None:
            handler.wfile.write(chunk)
            handler.wfile.flush()

        try:
            streams.pump(proc, write_chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # client went away or startup timeout; pump() already killed ffmpeg

    def route_hls_playlist(self, handler, kind: str, query: dict):
        """Ensure the session exists and serve its playlist with absolute segment URLs."""
        if kind == "live":
            channel = query.get("ch", ["101"])[0] or "101"
            if channel not in CHANNELS:
                handler.send_error(404)
                return
            session = streams.ensure_live(channel)
        elif kind == "play":
            from_cam = (query.get("from", [""])[0] or "").strip()
            to_cam = (query.get("to", [""])[0] or "").strip()
            if not (util._CAM_RE.match(from_cam) and util._CAM_RE.match(to_cam)):
                handler.send_error(400, "bad clip window")
                return
            session = streams.ensure_play(from_cam, to_cam)
        else:
            handler.send_error(404)
            return
        key = session["key"]

        playlist_path = session["dir"] / "index.m3u8"
        if not playlist_path.is_file():
            import time as _time

            deadline = _time.monotonic() + 5.0
            while not playlist_path.is_file() and _time.monotonic() < deadline:
                _time.sleep(0.25)
        if not playlist_path.is_file():
            handler.send_error(503, "stream starting - retry in a moment")
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

    def route_hls(self, handler, path: str, query: dict):
        # /hls/<key>/index.m3u8  |  /hls/media/<key>/<segment>
        parts = [p for p in path.split("/") if p][1:]  # drop 'hls'
        if len(parts) == 2 and parts[1] == "index.m3u8":
            return self.route_hls_playlist(handler, parts[0], query)
        if len(parts) == 3 and parts[0] == "media":
            return self.route_hls_media(handler, parts[1], parts[2])
        handler.send_error(404)

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
        parsed = urlparse(handler.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        if path == "/":
            return self.page_live(handler)
        if path == "/recordings":
            return self.page_recordings(handler, query)
        if path == "/play":
            return self.page_play(handler, query)
        if path.startswith("/hls/"):
            return self.route_hls(handler, path, query)
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

    return Handler


def run() -> None:
    import atexit
    import signal

    app = ViewerApp()

    def _shutdown(*_args) -> None:
        streams.kill_all()

    atexit.register(_shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

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
