"""Central configuration, read once from the environment (or config.env via run.sh)."""

import os

from . import config_file


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Optional JSON camera file (see config_file): one camera per instance. When set,
# the file wins for the fields it sets and HIK_* fills the rest.
VIEWER_CONFIG = os.environ.get(config_file.VIEWER_CONFIG_ENV, "").strip()

# A missing or invalid file is captured here and reported by app.run(), which
# refuses to start rather than quietly serving defaults. The constants below
# still fall back to HIK_*/defaults so importing this module never raises.
CONFIG_ERROR: str | None = None
_file_fields: config_file.CameraFields = {}
if VIEWER_CONFIG:
    try:
        _file_fields = config_file.load(VIEWER_CONFIG)
    except config_file.ConfigFileError as exc:
        CONFIG_ERROR = str(exc)

_settings, CONFIG_NOTES = config_file.resolve_camera(os.environ, _file_fields)

CAM_HOST = _settings.host
CAM_USER = _settings.user
CAM_PASS = _settings.password
HTTP_PORT = _settings.http_port
RTSP_PORT = _settings.rtsp_port
CAM_TLS = _settings.tls
CAM_RTSP_TLS = _settings.rtsp_tls
CAM_TLS_VERIFY = _settings.tls_verify
ISAPI_SCHEME = "https" if CAM_TLS else "http"
# rtsp_tls defaults to tls, so an older file without it keeps both services on
# the same scheme; the two can differ when ISAPI is HTTPS but RTSPS is absent.
RTSP_SCHEME = "rtsps" if CAM_RTSP_TLS else "rtsp"
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
CAM_BASE = config_file.isapi_base(_settings)
RTSP_BASE = config_file.rtsp_base(_settings)
# requests' certificate verification follows tls_verify; it only matters when
# the camera is reached over https (tls: true).
CAM_VERIFY = CAM_TLS_VERIFY
ISAPI_TIMEOUT = 8.0
