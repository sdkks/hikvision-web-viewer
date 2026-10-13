"""Server-rendered HTML templates (string.Template, no framework)."""

from string import Template

BASE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>$title · Camera viewer</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<header class="top"><span class="brand">$brand</span>
<nav class="tabs" aria-label="Sections">
<a href="/" aria-current="$nav_live">Live</a>
<a href="/recordings" aria-current="$nav_rec">Recordings</a>
</nav></header>
<main>
$content
</main>
<footer class="foot"><span>$footer</span><span>Advanced: <a href="/isapi/System/deviceInfo">raw ISAPI</a></span></footer>
<script src="/static/vendor/hls.light.min.js"></script>
<script src="/static/app.js?v=8"></script>
</body>
</html>
""")

LIVE = """<h1 style="position:absolute;left:-9999px">Live view</h1>
<div class="videowrap">
<video id="live-video" data-channel="$channel" data-codecs="$codec_set" autoplay muted playsinline controls
       poster="/snapshot?ch=$channel"></video>
</div>
<div class="channelbar" role="group" aria-label="Channel">
<button type="button" class="chan" data-channel="101" aria-pressed="$p_main">Main 1080p</button>
<button type="button" class="chan" data-channel="102" aria-pressed="$p_sub">Sub 360p &middot; lighter</button>
</div>
<p id="status-line" class="status" role="status"><span class="dot spin"></span><span class="label">Connecting&hellip;</span></p>
<div class="fallback">
<noscript><p>JavaScript is off, so live video can't load. Still images work:</p></noscript>
<a href="/snapshot?ch=101">Still image (main)</a>
<a href="/snapshot?ch=102">Still image (sub)</a>
</div>
<button id="retry" type="button" class="retry" hidden>Retry now</button>
"""

RECORDINGS = """<h1>Recordings</h1>
<div class="presets" aria-label="Quick ranges">
<a href="/recordings?preset=today">Today</a>
<a href="/recordings?preset=24h">Last 24 h</a>
<a href="/recordings?preset=3d">Last 3 days</a>
<a href="/recordings?preset=7d">Last 7 days</a>
</div>
<details class="range" $details_open>
<summary>Custom range</summary>
<form class="search" method="get" action="/recordings">
<div class="fieldrow">
<div class="field"><label for="from">From</label><input id="from" name="from" type="datetime-local" value="$from_val" required></div>
<div class="field"><label for="to">To</label><input id="to" name="to" type="datetime-local" value="$to_val" required></div>
<button type="submit" class="searchbtn">Search</button>
</div>
</form>
</details>
$results
"""

PLAY = Template("""<a class="back" href="/recordings">&larr; Results</a>
<h1>Clip &middot; $start_local &rarr; $end_local <span class="muted">($dur_h$size_part)</span></h1>
<div class="videowrap">
<video id="clip-video" autoplay muted playsinline controls data-codecs="hevc"
       data-url="/hls/play/index.m3u8?from=$from_cam&amp;to=$to_cam" data-expected="$dur_s"></video>
</div>
<p class="honest">Plays in real time &mdash; a $dur_h clip takes $dur_h. No seeking.</p>
<p class="fallback"><a href="/snapshot?ch=101">Still image (main)</a><a href="/snapshot?ch=102">Still image (sub)</a></p>
<p id="status-line" class="status" role="status"><span class="dot spin"></span><span class="label">Connecting to clip&hellip; starts at its beginning.</span></p>
<p id="counter" class="counter muted">00:00 / $dur_mmss</p>
<div class="progresswrap" aria-hidden="true"><div id="progress" class="progress"></div></div>
<div id="endnote" class="endnote" hidden></div>
""")
