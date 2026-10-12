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
BIND_HOST = os.environ.get("VIEWER_BIND", "127.0.0.1")
BIND_PORT = _int("VIEWER_PORT", 8756)
CAM_BASE = f"http://{CAM_HOST}:{HTTP_PORT}"
RTSP_BASE = f"rtsp://{CAM_HOST}:{RTSP_PORT}"
ISAPI_TIMEOUT = 8.0
