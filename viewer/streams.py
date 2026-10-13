"""ffmpeg HLS session manager.

Every session transcodes the camera's HEVC to H.264 (browsers cannot MSE-decode
HEVC reliably; H.264 plays everywhere) and writes an HLS playlist + MPEG-TS
segments into a per-session directory. One session per live channel, one for
playback (the camera enforces a single playback session itself).

An idle reaper kills sessions nobody has polled recently and deletes their files.
"""

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import camera, config

HLS_ROOT = Path(tempfile.gettempdir()) / f"hikvision-viewer-hls-{config.BIND_PORT}"
LIVE_IDLE_S = 90
PLAY_IDLE_S = 240

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()
_reaper_lock = threading.Lock()
_reaper_started = False


def _ffmpeg_base() -> list[str]:
    return ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp"]


def _tls_input_options() -> list[str]:
    """ffmpeg TLS input options, added only when RTSP is configured for TLS.

    These are input options for the RTSP connection, not for ISAPI, so they track
    ``rtsp_tls`` (not ``tls``). ``-tls_verify`` is ffmpeg's tls-protocol option
    (verified with ``ffmpeg -h protocol=tls``); the RTSP-TLS-off path adds
    nothing, so the default command stays byte-identical to before.
    """
    if not config.CAM_RTSP_TLS:
        return []
    return ["-tls_verify", "1" if config.CAM_TLS_VERIFY else "0"]


def _hls_out(directory: Path, live: bool, transcode: bool) -> list[str]:
    """HLS output flags.

    transcode=True: HEVC -> H.264 (web-safe, costs CPU).
    transcode=False: source is already H.264 -> '-c:v copy' (near-zero CPU).
    """
    flags = "delete_segments+independent_segments" if live else "independent_segments"
    codec_flags = (
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p", "-g", "25"]
        if transcode
        else ["-c", "copy"]
    )
    return codec_flags + [
        "-an",
        "-f",
        "hls",
        "-hls_time",
        "1" if live else "2",
        "-hls_list_size",
        "6" if live else "0",
        "-hls_flags",
        flags,
        "-hls_segment_filename",
        str(directory / "seg_%05d.ts"),
        str(directory / "index.m3u8"),
    ]


def _camera_rtsp(channel: str) -> str:
    creds = f"{config.CAM_USER}:{config.CAM_PASS}@"
    return (
        f"{config.RTSP_SCHEME}://{creds}{config.CAM_HOST}:{config.RTSP_PORT}"
        f"/Streaming/channels/{channel}"
    )


def _live_cmd(channel: str, directory: Path, transcode: bool) -> list[str]:
    if config.MOCK:
        return [
            "ffmpeg",
            "-loglevel",
            "error",
            # Pace like a real camera: without -re the synthetic source encodes many
            # times faster than realtime, so the HLS timeline races and hls.js chases
            # a runaway live edge (while burning CPU on content nobody can watch).
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=15",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "30",
            "-an",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "6",
            "-hls_flags",
            "delete_segments+independent_segments",
            "-hls_segment_filename",
            str(directory / "seg_%05d.ts"),
            str(directory / "index.m3u8"),
        ]
    return (
        _ffmpeg_base()
        + _tls_input_options()
        + [
            "-i",
            _camera_rtsp(channel),
        ]
        + _hls_out(directory, live=True, transcode=transcode)
    )


def _play_cmd(from_cam: str, to_cam: str, directory: Path, transcode: bool) -> list[str]:
    if config.MOCK:
        return [
            "ffmpeg",
            "-loglevel",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=15",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "30",
            "-an",
            "-t",
            "120",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "0",
            "-hls_flags",
            "independent_segments",
            "-hls_segment_filename",
            str(directory / "seg_%05d.ts"),
            str(directory / "index.m3u8"),
        ]
    from urllib.parse import quote

    creds = f"{config.CAM_USER}:{config.CAM_PASS}@"
    q = quote(f"starttime={from_cam}&endtime={to_cam}", safe="=&")
    return (
        _ffmpeg_base()
        + _tls_input_options()
        + [
            "-i",
            f"{config.RTSP_SCHEME}://{creds}{config.CAM_HOST}:{config.RTSP_PORT}"
            f"/Streaming/tracks/101?{q}",
        ]
        + _hls_out(directory, live=False, transcode=transcode)
    )


def live_key(channel: str) -> str:
    return f"live-{channel}"


def play_key(from_cam: str, to_cam: str) -> str:
    digest = hashlib.md5(f"{from_cam}-{to_cam}".encode()).hexdigest()[:10]
    return f"play-{digest}"


def _kill_orphans(directory: Path) -> None:
    """Best-effort: kill leftover ffmpeg processes writing into this session dir.

    Belt and braces only — the parent watchdog already guarantees children die
    with the server. `pkill` may be absent (minimal container images), so never
    let this failure break a session start.
    """
    try:
        subprocess.run(["pkill", "-9", "-f", str(directory)], check=False, capture_output=True)
    except OSError:
        pass  # no pkill here; the watchdog covers orphan cleanup


def _spawn(kind: str, key: str, cmd: list[str], directory: Path) -> dict:
    """Spawn ffmpeg under a watchdog shell.

    The watchdog supervises ffmpeg:
      - if the server process dies, it SIGKILLs and reaps ffmpeg (macOS has no
        PDEATHSIG), so a crashed server cannot leave encoder orphans behind;
      - on TERM/INT it kills and reaps its child before exiting, so no zombie
        is reparented to PID 1 (which in a container is this app and never
        reaps stray children).
    Runs in its own session so the group can be killed atomically.
    """
    err_log = open(directory / "ffmpeg-stderr.log", "ab")
    watchdog = (
        'child=""; '
        'reap() { if [ -n "$child" ]; then kill -9 "$child" 2>/dev/null; wait "$child" 2>/dev/null; fi; }; '
        'trap "reap; exit 143" TERM INT HUP; '
        "parent=$PPID; "
        '"$@" & child=$!; '
        'while kill -0 "$parent" 2>/dev/null; do '
        '  kill -0 "$child" 2>/dev/null || { wait "$child" 2>/dev/null; exit 0; }; '
        "  sleep 2; "
        "done; "
        "reap"
    )
    wrapped = ["/bin/sh", "-c", watchdog, "ffmpeg-watchdog"] + cmd
    return {
        "kind": kind,
        "key": key,
        "proc": subprocess.Popen(
            wrapped, stdout=subprocess.DEVNULL, stderr=err_log, start_new_session=True
        ),
        "dir": directory,
        "err_log": err_log,
        "last_access": time.monotonic(),
        "started": time.time(),
    }


def needs_transcode(codec: str) -> bool:
    """H.264 copies verbatim (web-safe); anything else (HEVC, unknown) transcodes."""
    return codec != "H.264"


def ensure_live(channel: str, transcode: bool) -> dict:
    """Start or refresh the live HLS session for a channel."""
    key = live_key(channel)
    with _sessions_lock:
        session = _sessions.get(key)
        if session and session["proc"].poll() is None:
            session["last_access"] = time.monotonic()
            return session
        if session:
            shutil.rmtree(session["dir"], ignore_errors=True)
        directory = HLS_ROOT / key
        _kill_orphans(directory)
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        session = _spawn("live", key, _live_cmd(channel, directory, transcode), directory)
        _sessions[key] = session
        _ensure_reaper()
        return session


def ensure_play(from_cam: str, to_cam: str, transcode: bool) -> dict:
    """Start or refresh the playback HLS session (single slot: kills previous)."""
    key = play_key(from_cam, to_cam)
    with _sessions_lock:
        for other_key, other in list(_sessions.items()):
            if other_key.startswith("play-") and other_key != key:
                _kill(other)
                shutil.rmtree(other["dir"], ignore_errors=True)
                del _sessions[other_key]
        session = _sessions.get(key)
        if session and session["proc"].poll() is None:
            session["last_access"] = time.monotonic()
            return session
        if session:
            shutil.rmtree(session["dir"], ignore_errors=True)
        directory = HLS_ROOT / key
        _kill_orphans(directory)
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        session = _spawn("play", key, _play_cmd(from_cam, to_cam, directory, transcode), directory)
        _sessions[key] = session
        _ensure_reaper()
        return session


def stop_play() -> bool:
    """Stop the (single) playback session."""
    stopped = False
    with _sessions_lock:
        for key in [k for k in _sessions if k.startswith("play-")]:
            session = _sessions.pop(key)
            _kill(session)
            shutil.rmtree(session["dir"], ignore_errors=True)
            stopped = True
    return stopped


def stop_key(kind: str, ident: str) -> bool:
    """Stop a session on explicit request (viewer gone / tab hidden)."""
    key = live_key(ident) if kind == "live" else play_key(ident, ident)
    with _sessions_lock:
        session = _sessions.pop(key, None)
    if not session:
        return False
    _kill(session)
    shutil.rmtree(session["dir"], ignore_errors=True)
    return True


def get_session(key: str) -> dict | None:
    """Touch and return a session if it is still alive."""
    with _sessions_lock:
        session = _sessions.get(key)
        if session and session["proc"].poll() is None:
            session["last_access"] = time.monotonic()
            return session
        return None


def session_dir(key: str) -> Path | None:
    with _sessions_lock:
        session = _sessions.get(key)
        return session["dir"] if session else None


def kill_all() -> None:
    """Kill every tracked session (server shutdown)."""
    with _sessions_lock:
        for session in _sessions.values():
            _kill(session)
        _sessions.clear()


def _kill(session: dict) -> None:
    try:
        session.get("err_log") and session["err_log"].close()
    except Exception:
        pass
    proc = session["proc"]
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)  # watchdog + ffmpeg
        except ProcessLookupError, PermissionError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError, PermissionError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass


def _reap_loop() -> None:
    while True:
        time.sleep(15)
        now = time.monotonic()
        with _sessions_lock:
            for key, session in list(_sessions.items()):
                idle_limit = LIVE_IDLE_S if session["kind"] == "live" else PLAY_IDLE_S
                if now - session["last_access"] > idle_limit or session["proc"].poll() is not None:
                    _kill(session)
                    shutil.rmtree(session["dir"], ignore_errors=True)
                    del _sessions[key]


def _ensure_reaper() -> None:
    global _reaper_started
    with _reaper_lock:
        if not _reaper_started:
            thread = threading.Thread(target=_reap_loop, daemon=True)
            thread.start()
            _reaper_started = True


def mock_snapshot_jpeg() -> bytes:
    """One-shot test pattern JPEG for mock mode."""
    out = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=1",
            "-frames:v",
            "1",
            "-q:v",
            "5",
            "-f",
            "image2",
            "-",
        ],
        capture_output=True,
        timeout=15,
    )
    if out.returncode != 0 or not out.stdout.startswith(b"\xff\xd8"):
        stderr = (out.stderr or b"").decode("utf-8", "replace").strip()
        raise camera.BadResponseError(
            "mock snapshot generation failed "
            f"(ffmpeg exit {out.returncode}): {stderr or 'ffmpeg produced no image'}"
        )
    return out.stdout


class MockDownloadResponse:
    """Tiny stand-in for a requests streaming response (mock mode only)."""

    status_code = 200
    headers = {"Content-Type": "video/mp4"}

    def iter_content(self, chunk_size):
        out = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:rate=15",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-t",
                "5",
                "-movflags",
                "frag_keyframe+empty_moov+default_base_moof",
                "-f",
                "mp4",
                "pipe:1",
            ],
            capture_output=True,
            timeout=60,
        )
        # Mirror the snapshot path: a failed encoder is a failed reply, not an
        # empty file. Never yield nothing and let the caller call that a 200.
        if out.returncode != 0 or not out.stdout:
            stderr = (out.stderr or b"").decode("utf-8", "replace").strip()
            detail = stderr or "ffmpeg produced no output"
            raise camera.BadResponseError(
                f"mock download generation failed (ffmpeg exit {out.returncode}): {detail}"
            )
        data = out.stdout
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def close(self):
        pass


def mock_download_response():
    return MockDownloadResponse()
