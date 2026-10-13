#!/usr/bin/env bash
# Integration smoke test: exercises real session machinery against mock fixtures.
#
# Verifies: server starts, pages render, a live HLS session produces playable
# H.264 segments, the stop route kills the session, and no ffmpeg survives.
set -euo pipefail

export HIK_MOCK=1
export VIEWER_BIND=127.0.0.1
export VIEWER_PORT="${VIEWER_PORT:-8799}"
BASE="http://${VIEWER_BIND}:${VIEWER_PORT}"

python -m viewer.app &
SERVER_PID=$!
cleanup() {
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
}
trap cleanup EXIT

echo "== waiting for server"
for _ in $(seq 1 40); do
    if curl -sf -m 2 "${BASE}/healthz" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done
curl -sf -m 3 "${BASE}/healthz" | grep -q ok

echo "== page renders"
curl -sf -m 5 "${BASE}/" | grep -q "live-video"

echo "== live HLS session produces H.264 segments"
playlist="$(curl -sf -m 20 "${BASE}/hls/live/index.m3u8?ch=101")"
segment="$(printf '%s\n' "${playlist}" | grep -v '^#' | head -n 1)"
test -n "${segment}"
curl -sf -m 15 -o /tmp/ci-smoke-segment.ts "${BASE}${segment}"
ffprobe -v error -show_entries stream=codec_name -of csv=p=0 /tmp/ci-smoke-segment.ts | grep -qx h264

echo "== stop route ends the session"
curl -sf -m 5 "${BASE}/hls-live-stop?ch=101" | grep -q true
sleep 2
if pgrep -x ffmpeg >/dev/null 2>&1; then
    echo "FAIL: ffmpeg still running after stop" >&2
    pgrep -ax ffmpeg >&2 || true
    exit 1
fi

echo "== neighbours API"
neigh="$(curl -sf -m 10 "${BASE}/api/neighbours?from=20261012T194808Z&to=20261012T201036Z")"
printf '%s\n' "${neigh}" | grep -q '"earlier"'
printf '%s\n' "${neigh}" | grep -q '"window"'
echo "== neighbours API omits window on a non-ok state"
bad="$(curl -s -m 10 "${BASE}/api/neighbours?from=nope&to=nope")"
printf '%s\n' "${bad}" | grep -q 'bad_request'
if printf '%s\n' "${bad}" | grep -q '"window"'; then
    echo "FAIL: window returned for a non-ok state" >&2
    exit 1
fi

echo "== coverage API and picker markup"
curl -sf -m 10 "${BASE}/api/coverage?date=$(date +%F)" | grep -q '"state"'
curl -sf -m 5 "${BASE}/recordings" | grep -q "hour-grid"
curl -sf -m 5 "${BASE}/recordings" | grep -q "scrubber"

echo "== download route relays a body named from its bytes"
curl -sf -m 60 -D /tmp/ci-smoke-download-headers.txt -o /tmp/ci-smoke-download.mp4 \
    "${BASE}/download?from=20261012T194808Z&to=20261012T201036Z&name=ch01_x&size=1000"
test -s /tmp/ci-smoke-download.mp4
# The mock's ffmpeg muxer emits fragmented ISO base media, i.e. bytes 4-8 are
# the ftyp box. .mp4 / video/mp4 is therefore the honest name -- and the bytes
# must actually say so, not merely the filename.
test "$(dd if=/tmp/ci-smoke-download.mp4 bs=1 skip=4 count=4 2>/dev/null)" = "ftyp"
grep -qi '^content-disposition: attachment; filename="clip_20261012T194808Z_20261012T201036Z.mp4"' \
    /tmp/ci-smoke-download-headers.txt
grep -qi '^content-type: video/mp4' /tmp/ci-smoke-download-headers.txt

echo "== recordings page renders"
curl -sf -m 5 "${BASE}/recordings" | grep -q "Recordings"

echo "== rendered links resolve in mock mode"
python - "${BASE}" <<'PY'
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
import sys

base = sys.argv[1]
origin = urlsplit(base)
links = set()

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class Hrefs(HTMLParser):
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key != "href" or not value or value.startswith("#"):
                continue
            url = urljoin(self.page_url, value)
            target = urlsplit(url)
            if (target.scheme, target.netloc) == (origin.scheme, origin.netloc):
                links.add(url)

opener = build_opener(NoRedirect)
# MockCamera returns its fixture clip for this search, regardless of wall-clock date.
for path in ("/", "/recordings?preset=7d", "/motion"):
    with opener.open(base + path, timeout=20) as response:
        assert response.status == 200, (path, response.status)
        Hrefs(base + path).feed(response.read().decode("utf-8"))
assert any(urlsplit(url).path == "/download" for url in links), "no Download link rendered"
for url in sorted(links):
    print("checking", url, flush=True)
    with opener.open(url, timeout=20) as response:
        assert response.status == 200, (url, response.status)
        body = response.read()
        assert body, (url, "empty response")
        if urlsplit(url).path == "/download":
            assert response.headers.get_content_type() == "video/mp4", url
            assert response.headers.get("Content-Disposition", "").startswith("attachment;"), url
print(f"rendered-link sweep passed ({len(links)} links)")
PY

echo "smoke test passed"
