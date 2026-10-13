"""ISAPI client for the camera, with an explicit error model and a mock provider.

Error model (maps 1:1 to the UI's honest states):
  CameraError          - base class
  UnreachableError     - connection refused / timeout / DNS: the device itself is not there
  ServiceDownError     - device reachable but the asked-for app-layer service failed
                         (HTTP 403/500, timeout-after-connect, unparseable reply)
  BadResponseError     - unexpected reply shape (protocol drift)
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree

import requests
from requests.auth import HTTPDigestAuth

from . import config, util


class CameraError(Exception):
    """Base class."""


class UnreachableError(CameraError):
    pass


class ServiceDownError(CameraError):
    pass


class BadResponseError(CameraError):
    pass


@dataclass(frozen=True)
class Clip:
    """One recorded clip as reported by the search service."""

    uri: str  # normalized rtsp playbackURI (unescaped, for display/VLC)
    uri_escaped: (
        str  # XML-escaped playbackURI exactly as the camera returned it (for ISAPI download)
    )
    start_utc: str  # 20261012T194808Z
    end_utc: str
    name: str
    size: int
    duration_s: int

    @property
    def start_dt(self):
        return util.parse_cam_time(self.start_utc)

    @property
    def end_dt(self):
        return util.parse_cam_time(self.end_utc)


def _classify_requests_error(exc: Exception) -> CameraError:
    # Connect failures (including ConnectTimeout) mean the host/port is not
    # reachable; a slow-but-connected reply is a service problem.
    if isinstance(exc, requests.exceptions.ConnectionError):
        return UnreachableError(f"cannot connect to the camera: {exc}")
    if isinstance(exc, requests.exceptions.Timeout):
        return ServiceDownError(f"camera did not answer in time: {exc}")
    if isinstance(exc, requests.exceptions.RequestException):
        return UnreachableError(f"camera request failed: {exc}")
    return CameraError(str(exc))


class Camera:
    """Talks to the real device over ISAPI."""

    def __init__(self, host_base: str, user: str, password: str, rtsp_base: str, timeout: float):
        self.base = host_base
        self.rtsp_base = rtsp_base
        self.auth = HTTPDigestAuth(user, password)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = self.auth

    def _get(self, path: str) -> str:
        try:
            r = self.session.get(self.base + path, timeout=self.timeout)
        except Exception as exc:
            raise _classify_requests_error(exc) from exc
        if r.status_code == 200:
            return r.text
        raise ServiceDownError(f"{path} -> HTTP {r.status_code}")

    # ── info endpoints ───────────────────────────────────────────────────
    def device_info(self) -> dict:
        xml = self._get("/ISAPI/System/deviceInfo")
        return {
            tag: _xml_text(xml, tag)
            for tag in ("model", "SerialNumber", "firmwareVersion", "firmwareReleasedDate")
        }

    def status(self) -> dict:
        xml = self._get("/ISAPI/System/status")
        out = {
            tag: _xml_text(xml, tag)
            for tag in ("deviceUpTime", "cpuUtilization", "memoryUsage", "memoryAvailable")
        }
        out["uptime_s"] = int(out["deviceUpTime"]) if out["deviceUpTime"].isdigit() else 0
        return out

    def storage(self) -> dict:
        xml = self._get("/ISAPI/ContentMgmt/Storage")
        return parse_storage_response(xml)

    def search(self, iso_from: str, iso_to: str, max_results: int = 100) -> list[Clip]:
        """Search recordings on track 101. Raises ServiceDownError when the service is flaky."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<CMSearchDescription>"
            "<searchID>00000000-0000-0000-0000-000000000001</searchID>"
            "<trackList><trackID>101</trackID></trackList>"
            f"<timeSpanList><timeSpan><startTime>{iso_from}</startTime>"
            f"<endTime>{iso_to}</endTime></timeSpan></timeSpanList>"
            f"<maxResults>{max_results}</maxResults>"
            "</CMSearchDescription>"
        )
        try:
            r = self.session.post(
                self.base + "/ISAPI/ContentMgmt/search",
                data=body.encode(),
                headers={"Content-Type": "application/xml"},
                timeout=self.timeout,
            )
        except Exception as exc:
            raise _classify_requests_error(exc) from exc
        if r.status_code != 200:
            raise ServiceDownError(f"search -> HTTP {r.status_code}")
        return parse_search_response(r.text, self.rtsp_base)

    def snapshot_jpeg(self, channel: str) -> bytes:
        try:
            r = self.session.get(
                f"{self.base}/ISAPI/Streaming/channels/{channel}/picture",
                timeout=self.timeout,
            )
        except Exception as exc:
            raise _classify_requests_error(exc) from exc
        if r.status_code != 200 or not r.content.startswith(b"\xff\xd8"):
            raise ServiceDownError(f"snapshot ch{channel} -> HTTP {r.status_code}")
        return r.content

    def download_stream(self, escaped_uri: str):
        """Stream a recorded file over HTTP via /ISAPI/ContentMgmt/download.

        Returns the raw requests response (stream=True); caller iterates it.
        NOTE: escaped_uri must keep XML escaping (&amp;) — raw '&' breaks the XML body.
        """
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<downloadRequest version="1.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<playbackURI>{escaped_uri}</playbackURI>"
            "</downloadRequest>"
        )
        try:
            return self.session.get(
                self.base + "/ISAPI/ContentMgmt/download",
                data=body.encode(),
                headers={"Content-Type": "application/xml"},
                timeout=(self.timeout, 30),
                stream=True,
            )
        except Exception as exc:
            raise _classify_requests_error(exc) from exc

    def stream_codec(self, channel: str) -> str:
        """Reported encoding of a streaming channel: 'H.264', 'H.265', ... ('' if unknown).

        Used to decide copy vs transcode for the HLS session. Empty string on any
        failure -> callers default to transcoding (always web-safe).
        """
        try:
            xml = self._get(f"/ISAPI/Streaming/channels/{channel}")
        except CameraError:
            return ""
        codec = _xml_text(xml, "videoCodecType").upper()
        return codec if codec in ("H.264", "H.265", "H.265+") else ""

    def stream_resolution(self, channel: str) -> tuple[int, int] | None:
        xml = self._get(f"/ISAPI/Streaming/channels/{channel}")
        w, h = _int_text(xml, "videoResolutionWidth"), _int_text(xml, "videoResolutionHeight")
        return (w, h) if w and h else None

    def rtsp_live_url(self, channel: str) -> str:
        return f"{self.rtsp_base}/Streaming/channels/{channel}"

    def rtsp_playback_url(self, start_cam: str, end_cam: str) -> str:
        from urllib.parse import quote

        q = quote(f"starttime={start_cam}&endtime={end_cam}", safe="=&")
        return f"{self.rtsp_base}/Streaming/tracks/101?{q}"


class MockCamera(Camera):
    """Deterministic fixtures: same interface, no device.

    Note: stream/snapshot generation for mock mode lives in streams.py;
    this class covers the ISAPI-shaped endpoints.
    """

    MOCK_CLIP = (
        "rtsp://{host}:554/Streaming/tracks/101/?starttime=20261012T194808Z"
        "&endtime=20261012T201036Z&name=ch01_00000000000000000&size=264532808"
    )

    def device_info(self) -> dict:
        return {
            "model": "DS-2CV2121G2-IDW (mock)",
            "SerialNumber": "MOCKSERIAL20241107AAWRFS0000001",
            "firmwareVersion": "V5.7.26",
            "firmwareReleasedDate": "build260827",
        }

    def status(self) -> dict:
        return {
            "uptime_s": 4242,
            "cpuUtilization": "37",
            "memoryUsage": "81",
            "memoryAvailable": "8123",
            "deviceUpTime": "4242",
        }

    def storage(self) -> dict:
        return {"status": "ok", "capacity_mb": 244016, "free_mb": 231500, "writable": True}

    def stream_codec(self, channel: str) -> str:
        return "H.264"  # mock fixtures encode H.264 (exercises the copy path)

    def stream_resolution(self, channel: str) -> tuple[int, int] | None:
        return (1280, 720) if channel.endswith("1") else (640, 360)

    def search(self, iso_from: str, iso_to: str, max_results: int = 100) -> list[Clip]:
        uri = self.MOCK_CLIP.format(host=config.CAM_HOST)
        fields = util.clip_fields(uri)
        return [
            Clip(
                uri=uri,
                uri_escaped=(
                    f"rtsp://{config.CAM_HOST}:554/Streaming/tracks/101/"
                    "?starttime=20261012T194808Z&amp;endtime=20261012T201036Z"
                    "&amp;name=ch01_00000000000000000&amp;size=264532808"
                ),
                start_utc=fields["starttime"],
                end_utc=fields["endtime"],
                name=fields["name"],
                size=fields["size"],
                duration_s=fields["duration_s"],
            )
        ]

    def snapshot_jpeg(self, channel: str) -> bytes:
        from . import streams

        return streams.mock_snapshot_jpeg()

    _CANNED = {
        "/ISAPI/System/deviceInfo": (
            "<DeviceInfo><model>DS-2CV2121G2-IDW</model>"
            "<SerialNumber>MOCKSERIAL20241107AAWRFS0000001</SerialNumber>"
            "<firmwareVersion>V5.7.26</firmwareVersion>"
            "<firmwareReleasedDate>build260827</firmwareReleasedDate></DeviceInfo>"
        ),
        "/ISAPI/System/status": (
            "<DeviceStatus><deviceUpTime>4242</deviceUpTime>"
            "<cpuUtilization>37</cpuUtilization><memoryUsage>81</memoryUsage>"
            "<memoryAvailable>8123</memoryAvailable></DeviceStatus>"
        ),
        "/ISAPI/ContentMgmt/Storage": (
            "<storage><hddList><hdd><id>1</id><status>ok</status>"
            "<capacity>244016</capacity><freeSpace>231500</freeSpace>"
            "<property>RW</property></hdd></hddList></storage>"
        ),
    }

    def _get(self, path: str) -> str:
        if path in self._CANNED:
            return self._CANNED[path]
        raise ServiceDownError(f"mock has no fixture for {path}")


def _first_element(xml_text: str, local_name: str):
    """First element with this local name, ignoring XML namespaces/attributes.

    Camera firmware varies: elements may carry attributes and be namespaced, so
    naive string matching (`<hdd>`) breaks. Returns None if absent/unparseable.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == local_name:
            return element
    return None


def parse_storage_response(xml_text: str) -> dict:
    """Parse /ISAPI/ContentMgmt/Storage into capacity/free/status/writable."""
    hdd = _first_element(xml_text, "hdd")
    if hdd is None:
        raise BadResponseError("no <hdd> element in storage reply")
    values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in hdd}
    capacity = values.get("capacity", "")
    free = values.get("freeSpace", "")
    if not capacity.isdigit():
        raise BadResponseError("storage reply has no capacity")
    return {
        "status": values.get("status", ""),
        "capacity_mb": int(capacity),
        "free_mb": int(free) if free.isdigit() else 0,
        "writable": values.get("property", "") == "RW",
    }


def parse_search_response(xml_text: str, rtsp_base: str) -> list[Clip]:
    """Parse /ISAPI/ContentMgmt/search replies; raise BadResponseError on protocol drift."""
    if "<numOfMatches>" not in xml_text:
        raise BadResponseError("search reply lacks numOfMatches")
    clips: list[Clip] = []
    for block in re.findall(r"<searchMatchItem>(.*?)</searchMatchItem>", xml_text, re.S):
        uri_raw = re.search(r"<playbackURI>(.*?)</playbackURI>", block, re.S)
        start = re.search(r"<startTime>(.*?)</startTime>", block, re.S)
        end = re.search(r"<endTime>(.*?)</endTime>", block, re.S)
        if not (uri_raw and start and end):
            continue
        uri = util.normalize_playback_uri(uri_raw.group(1), rtsp_base)
        uri_escaped = util.normalize_playback_uri(uri_raw.group(1), rtsp_base, unescape=False)
        fields = util.clip_fields(uri)
        start_utc = fields.get("starttime") or _iso_to_cam(start.group(1))
        end_utc = fields.get("endtime") or _iso_to_cam(end.group(1))
        duration = fields["duration_s"]
        if not duration and util._CAM_RE.match(start_utc) and util._CAM_RE.match(end_utc):
            delta = util.parse_cam_time(end_utc) - util.parse_cam_time(start_utc)
            duration = max(0, int(delta.total_seconds()))
        clips.append(
            Clip(
                uri=uri,
                uri_escaped=uri_escaped,
                start_utc=start_utc,
                end_utc=end_utc,
                name=fields.get("name", ""),
                size=fields["size"],
                duration_s=duration,
            )
        )
    return clips


def _iso_to_cam(iso: str) -> str:
    """ISO datetime (from search XML) -> camera string; '' when unparseable."""
    try:
        cleaned = iso.strip().replace(" ", "T")
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return util.to_cam_time(dt)
    except ValueError:
        return ""


def _xml_text(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml, re.S)
    return m.group(1).strip() if m else ""


def _int_text(xml: str, tag: str) -> int:
    value = _xml_text(xml, tag)
    return int(value) if value.isdigit() else 0


def make_camera() -> Camera:
    if config.MOCK:
        return MockCamera(
            config.CAM_BASE,
            config.CAM_USER,
            config.CAM_PASS,
            config.RTSP_BASE,
            config.ISAPI_TIMEOUT,
        )
    return Camera(
        config.CAM_BASE, config.CAM_USER, config.CAM_PASS, config.RTSP_BASE, config.ISAPI_TIMEOUT
    )
