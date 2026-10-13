"""Time helpers: camera stores UTC in formats like 20261012T194808Z; users think in local time."""

import re
from datetime import datetime, timedelta, timezone
from typing import TypedDict

CAM_FMT = "%Y%m%dT%H%M%SZ"
SEARCH_OUT_FMT = "%Y-%m-%dT%H:%M:%SZ"
_DT_LOCAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")
_CAM_RE = re.compile(r"^\d{8}T\d{6}Z$")
# A URL's userinfo. ffmpeg echoes the input URL it could not open, and this
# project's RTSP URLs carry the camera credentials, so ffmpeg's own words have to
# pass through this before a person reads them.
_URL_USERINFO_RE = re.compile(r"(?P<scheme>rtsps?|https?)://[^/\s]*@")


def parse_cam_time(value: str) -> datetime:
    """20261012T194808Z -> aware UTC datetime. Raises ValueError on garbage."""
    return datetime.strptime(value.strip(), CAM_FMT).replace(tzinfo=timezone.utc)


def to_cam_time(dt: datetime) -> str:
    """Aware datetime -> camera string (UTC)."""
    return dt.astimezone(timezone.utc).strftime(CAM_FMT)


def parse_user_range(raw_from: str, raw_to: str) -> tuple[datetime, datetime]:
    """Parse datetime-local style input, treating naive values as server-local time.

    Returns aware datetimes (from, to). Raises ValueError on bad input or from >= to.
    """
    parsed = []
    for raw in (raw_from, raw_to):
        raw = (raw or "").strip()
        if not _DT_LOCAL_RE.match(raw):
            raise ValueError(f"invalid datetime: {raw!r}")
        if len(raw) == 16:
            raw += ":00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        parsed.append(dt)
    frm, to = parsed
    if frm >= to:
        raise ValueError("range start must be before range end")
    return frm, to


def iso_search_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(SEARCH_OUT_FMT)


def local_display(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %d %b %H:%M")


def human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def human_size(num_bytes: int) -> str:
    value = float(max(0, int(num_bytes)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def redact_url_credentials(text: str) -> str:
    """Replace URL userinfo with ``***:***`` so error text can be shown safely.

    The substitution keeps the scheme and host, which is what makes the message
    useful, and drops only the credentials. A path that merely contains ``@`` is
    left alone: userinfo cannot contain ``/``, so the match stops at the first
    one, and a password that itself contains ``@`` is covered because the match
    runs to the last ``@`` before the path.
    """
    return _URL_USERINFO_RE.sub(r"\g<scheme>://***:***@", text)


def normalize_playback_uri(uri: str, rtsp_base: str, unescape: bool = True) -> str:
    """Camera playbackURI: rewrite host:port, keep query. XML entities kept on request
    (the ISAPI download body needs the &amp;-escaped form verbatim)."""
    import html as html_mod
    from urllib.parse import urlparse

    uri = (html_mod.unescape(uri) if unescape else uri) or ""
    uri = uri.strip()
    if not uri:
        return ""
    base = urlparse(rtsp_base)
    return re.sub(r"^rtsps?://[^/?]+", f"{base.scheme}://{base.netloc}", uri)


class ClipFields(TypedDict):
    """The clip identity carried in a playbackURI query string."""

    starttime: str
    endtime: str
    name: str
    size: int
    duration_s: int


def clip_fields(uri: str) -> ClipFields:
    """Extract starttime/endtime/name/size from a playbackURI query string."""
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(uri).query)
    flat = {k: v[0] for k, v in q.items() if v}
    start = flat.get("starttime", "")
    end = flat.get("endtime", "")
    duration = 0
    if _CAM_RE.match(start) and _CAM_RE.match(end):
        delta = parse_cam_time(end) - parse_cam_time(start)
        duration = max(0, int(delta.total_seconds()))
    return {
        "starttime": start,
        "endtime": end,
        "name": flat.get("name", ""),
        "size": int(flat["size"]) if flat.get("size", "").isdigit() else 0,
        "duration_s": duration,
    }


def datetime_now() -> datetime:
    """Timezone-aware 'now' in the server's local timezone."""
    return datetime.now().astimezone()


def relative_preset(hours: float | None, midnight: bool) -> tuple[datetime, datetime]:
    """Return (from, to) aware datetimes for a named preset window."""
    now = datetime.now().astimezone()
    if midnight:
        frm = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        frm = now - timedelta(hours=hours or 24)
    return frm, now
