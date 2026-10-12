"""ffmpeg session manager.

Serves fragmented-MP4 live streams and live-tempo clip playback by piping ffmpeg
stdout into the HTTP response. Enforces the camera's one-playback-session limit by
killing any previous playback process when a new one starts.

All ffmpeg invocations are read-only against the camera.
"""

import subprocess
import threading

from . import config

CHUNK = 65536
STARTUP_TIMEOUT_S = 12.0

_live_lock = threading.Lock()
_playback_lock = threading.Lock()
_live_procs: dict[str, subprocess.Popen] = {}
_playback_proc: subprocess.Popen | None = None


def _ffmpeg_base() -> list[str]:
    return ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp"]


def _fmp4_out() -> list[str]:
    return [
        "-c",
        "copy",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]


def _mock_live_cmd(channel: str) -> list[str]:
    size = "1280x720" if channel.endswith("1") else "640x360"
    return [
        "ffmpeg",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate=15",
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
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]


def _mock_playback_cmd(duration_s: int) -> list[str]:
    duration = max(5, min(int(duration_s or 30), 600))
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
        "-t",
        str(duration),
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]


def live_cmd(channel: str) -> list[str]:
    """ffmpeg command for an endless live stream of the given ISAPI channel."""
    if config.MOCK:
        return _mock_live_cmd(channel)
    creds = f"{config.CAM_USER}:{config.CAM_PASS}@"
    source = f"rtsp://{creds}{config.CAM_HOST}:{config.RTSP_PORT}/Streaming/channels/{channel}"
    return _ffmpeg_base() + ["-i", source] + _fmp4_out()


def playback_cmd(rtsp_url: str, duration_s: int) -> list[str]:
    """ffmpeg command for a live-tempo playback of an archived clip window."""
    if config.MOCK:
        return _mock_playback_cmd(duration_s)
    return _ffmpeg_base() + ["-i", rtsp_url] + _fmp4_out()


def _kill_locked(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def start_live(channel: str) -> subprocess.Popen:
    """Start (or restart) the live ffmpeg session for one channel."""
    global _live_procs
    with _live_lock:
        old = _live_procs.get(channel)
        _kill_locked(old)
        proc = subprocess.Popen(
            live_cmd(channel), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
        )
        _live_procs[channel] = proc
        return proc


def start_playback(rtsp_url: str, duration_s: int) -> subprocess.Popen:
    """Start a playback session, enforcing the camera's single-playback limit."""
    global _playback_proc
    with _playback_lock:
        _kill_locked(_playback_proc)
        proc = subprocess.Popen(
            playback_cmd(rtsp_url, duration_s),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        _playback_proc = proc
        return proc


def stop_playback() -> None:
    global _playback_proc
    with _playback_lock:
        _kill_locked(_playback_proc)
        _playback_proc = None


def pump(proc: subprocess.Popen, write_chunk, timeout: float = STARTUP_TIMEOUT_S):
    """Yield nothing; instead push proc.stdout into write_chunk() until EOF/error.

    Kills ffmpeg when the client disconnects (write_chunk raises) and guards
    against a startup hang where ffmpeg produces no bytes at all.
    """
    import selectors
    import time

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    got_any = False
    try:
        while True:
            remaining = deadline - time.monotonic() if not got_any else 30.0
            events = sel.select(timeout=max(0.1, remaining))
            if not events:
                if not got_any and time.monotonic() >= deadline:
                    raise TimeoutError("stream produced no data in time")
                if proc.poll() is not None:
                    break
                continue
            chunk = proc.stdout.read(CHUNK)
            if not chunk:
                break
            got_any = True
            deadline = time.monotonic() + 30.0
            write_chunk(chunk)
    finally:
        sel.close()
        _kill_locked(proc)


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
        raise RuntimeError("mock snapshot generation failed")
    return out.stdout


def build_playback_url(from_cam: str, to_cam: str) -> str:
    """RTSP track URL for a time window (real camera; mock ignores the URL)."""
    from urllib.parse import quote

    q = quote(f"starttime={from_cam}&endtime={to_cam}", safe="=&")
    return f"rtsp://{config.CAM_HOST}:{config.RTSP_PORT}/Streaming/tracks/101?{q}"
