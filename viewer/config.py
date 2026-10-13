"""Central configuration, read once from the environment (or config.env via run.sh)."""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


CAM_HOST = os.environ.get("HIK_HOST", "192.168.0.156")
CAM_USER = os.environ.get("HIK_USER", "admin")
CAM_PASS = os.environ.get("HIK_PASSWORD", "")
RTSP_PORT = _int("HIK_RTSP_PORT", 554)
HTTP_PORT = _int("HIK_HTTP_PORT", 80)
MOCK = os.environ.get("HIK_MOCK", "") == "1"
DEFAULT_CHANNEL = os.environ.get("VIEWER_DEFAULT_CHANNEL", "101")
# Optional HTTP Basic Auth for the viewer itself. When both are set, every
# request requires credentials (browsers accept http://user:pass@host).
AUTH_USER = os.environ.get("VIEWER_AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("VIEWER_AUTH_PASSWORD", "")
BIND_HOST = os.environ.get("VIEWER_BIND", "127.0.0.1")
BIND_PORT = _int("VIEWER_PORT", 8756)
# Extra host names the viewer accepts in the Host header, for LAN access or a
# reverse proxy whose Host is not loopback. Comma-separated, case-insensitive;
# ports are ignored when matching. Loopback names and a specific BIND_HOST are
# always accepted, so this is only needed when the viewer is reached by another
# name (see the DNS-rebinding note in app.py).
ALLOWED_HOSTS = tuple(
    host.strip().lower()
    for host in os.environ.get("VIEWER_ALLOWED_HOSTS", "").split(",")
    if host.strip()
)
CAM_BASE = f"http://{CAM_HOST}:{HTTP_PORT}"
RTSP_BASE = f"rtsp://{CAM_HOST}:{RTSP_PORT}"
ISAPI_TIMEOUT = 8.0
