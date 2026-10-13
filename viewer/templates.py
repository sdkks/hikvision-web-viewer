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
<a href="/motion" aria-current="$nav_motion">Motion</a>
</nav></header>
<main>
$content
</main>
<footer class="foot"><span>$footer</span><span>Advanced: <a href="/isapi/System/deviceInfo">raw ISAPI</a></span></footer>
<script src="/static/vendor/hls.light.min.js"></script>
<script src="/static/app.js?v=9"></script>
</body>
</html>
""")

LIVE = """<h1 style="position:absolute;left:-9999px">Live view</h1>
<div class="videowrap">
<video id="live-video" data-channel="$channel" data-codecs="$codec_set" autoplay muted playsinline controls
       poster="/snapshot?ch=$channel"></video>
<button type="button" id="tap-start" class="tapstart" data-label="live view" hidden>Click or tap to play</button>
</div>
<div class="channelbar" role="group" aria-label="Channel">
<button type="button" class="chan" data-channel="101" aria-pressed="$p_main">$main_label</button>
<button type="button" class="chan" data-channel="102" aria-pressed="$p_sub">$sub_label</button>
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
<h2>Browse by time</h2>
<div class="picker" id="picker">
<div class="picker-head">
<label class="picker-day" for="pick-date">Jump to a day
<input type="date" id="pick-date" name="date">
</label>
</div>
<div class="chips" id="recent-days" aria-label="Recent days"></div>
<p id="picker-status" class="status" role="status"><span class="dot"></span><span class="label"></span></p>
<p><button type="button" id="picker-retry" class="retry" hidden>Retry this day</button></p>
<section id="day-view" hidden>
<h3 id="day-heading" tabindex="-1"></h3>
<p class="legend" aria-hidden="false">
<span><span class="swatch none"></span>no recording</span>
<span><span class="swatch part"></span>partial</span>
<span><span class="swatch full"></span>full</span>
<span><span class="swatch unknown"></span>couldn't check</span>
</p>
<div class="hourgrid" id="hour-grid" role="group" aria-label="Hours of the day"></div>
<h3 class="fileshead">Files on this day</h3>
<div class="files" id="day-files"></div>
</section>
<section id="hour-view" hidden>
<h3 id="hour-heading" tabindex="-1"></h3>
<div class="scrubwrap">
<p id="scrub-readout" class="muted"></p>
<input type="range" id="scrubber" min="0" max="59" step="1" value="0" aria-label="Minute of the hour" aria-describedby="scrub-readout">
</div>
<h3 class="fileshead">Files in this hour</h3>
<div class="files" id="hour-files"></div>
<div class="playbar" id="play-bar" hidden>
<a class="playbtn" id="play-from" href="#">&#9654; Play from</a>
</div>
<p class="muted" id="play-note" hidden>Playback starts a few seconds before the chosen minute (at the nearest keyframe).</p>
</section>
</div>
$results
"""

PLAY = Template("""<a class="back" href="/recordings">&larr; Results</a>
<h1>Clip &middot; $start_local &rarr; $end_local <span class="muted">($dur_h$size_part)</span></h1>
<div class="videowrap">
<video id="clip-video" autoplay muted playsinline controls data-codecs="hevc"
       data-url="/hls/play/index.m3u8?from=$from_cam&amp;to=$to_cam" data-expected="$dur_s"></video>
<button type="button" id="tap-start" class="tapstart" data-label="recording" hidden>Click or tap to play</button>
</div>
<p id="status-line" class="status" role="status"><span class="dot spin"></span><span class="label">Connecting to clip&hellip; starts at its beginning.</span></p>
<p id="counter" class="counter muted">Position unknown.</p>
<div class="restartbar" id="restart-bar" data-from-epoch="$from_epoch" data-window-from-epoch="$window_from_epoch" data-to-epoch="$to_epoch" data-size="$size_raw">
<div class="restartbar-head">
<label class="restartbar-label" for="restart-scrubber">Restart from</label>
<span class="muted" id="restart-ends">$start_local &rarr; $end_local</span>
</div>
<div class="restartbar-trackwrap">
<input type="range" id="restart-scrubber" min="$from_epoch" max="$to_epoch" step="5" value="$from_epoch"
       aria-describedby="restart-readout restart-caveat">
<span class="rb-now" id="restart-now" hidden aria-hidden="true"></span>
</div>
<p class="legend restartbar-legend">
<span><span class="swatch full"></span>recording</span>
<span><span class="swatch none"></span>no recording</span>
<span><span class="swatch unknown"></span>couldn&rsquo;t check</span>
</p>
<p class="restartbar-readout" id="restart-readout">Checking recorded coverage&hellip;</p>
<p class="muted" id="restart-caveat">Restarting starts a fresh playback session (not a seek) at the nearest keyframe before the chosen time &mdash; up to a few seconds earlier, with a short gap. You can go Back to your previous start time.</p>
<div class="restartbar-actions">
<button type="button" id="restart-go" class="searchbtn" disabled>Restart here</button>
<button type="button" id="restart-cancel" class="copybtn" hidden>Cancel</button>
</div>
</div>
<p class="honest">Plays in real time &mdash; a $dur_h clip takes $dur_h. No seeking.</p>
<p class="fallback"><span class="muted">Camera&rsquo;s current frame &mdash; a live still, not this clip:</span><a href="/snapshot?ch=101">Live still (main)</a><a href="/snapshot?ch=102">Live still (sub)</a></p>
<div id="endnote" class="endnote" hidden></div>
<section id="jumps" hidden>
<h2>Nearby recordings</h2>
<label class="autonext"><input type="checkbox" id="auto-next"> Play the next recording automatically</label>
<div class="jumpcols">
<div><h3>Earlier</h3><ul class="jumps" id="earlier"></ul></div>
<div><h3>Later</h3><ul class="jumps" id="later"></ul></div>
</div>
</section>
""")

MOTION = Template("""<h1>Motion detection</h1>
<p id="motion-status" class="status" role="status"><span class="dot spin"></span><span class="label">Reading the camera&hellip;</span></p>
<section id="motion-dirty" class="dirtybar" hidden>
<p id="motion-dirty-text">Changes not applied.</p>
<div class="row">
<button type="button" id="motion-apply-top" class="searchbtn">Apply to camera</button>
<button type="button" id="motion-discard-top" class="copybtn">Discard changes</button>
</div>
</section>
<div id="motion-error" class="errcard" hidden></div>
<noscript><div class="errcard"><p>Editing motion zones needs JavaScript. This page is read-only without it.</p>$ssr_summary</div></noscript>
<section class="zonecard">
<div class="zoneviewport" id="zone-viewport">
<div class="zonecanvas" id="zone-canvas">
<img id="zone-photo" src="/snapshot?ch=101" alt="Camera still frame used to place motion zones">
<div class="zonegrid" id="zone-grid" role="grid" aria-label="Motion zones, 18 rows by 22 columns" aria-rowcount="18" aria-colcount="22" tabindex="0" aria-activedescendant="zc-0-0"></div>
</div>
</div>
<p class="legend"><span><span class="swatch armed"></span>armed &mdash; motion here triggers</span><span><span class="swatch masked"></span>masked &mdash; ignored</span><span><span class="swatch cursor"></span>cursor</span></p>
<p class="muted" id="zone-hint" hidden>Zoom in to edit individual cells.</p>
</section>
<section class="zonequickcontrols" aria-label="Zone controls">
<fieldset class="zgroup">
<legend>Detection</legend>
<label class="ztoggle"><input type="checkbox" id="motion-enabled"> Motion detection on</label>
<p class="muted" id="motion-off-note" hidden>Motion detection is off &mdash; these zones do nothing until you turn it on.</p>
</fieldset>
<fieldset class="zgroup">
<legend>Tool</legend>
<div class="segmented" id="tool-group" role="group" aria-label="Paint tool">
<button type="button" class="segbtn" data-tool="arm" aria-pressed="true">Arm</button>
<button type="button" class="segbtn" data-tool="mask" aria-pressed="false">Mask</button>
</div>
</fieldset>
<fieldset class="zgroup">
<legend>Brush</legend>
<div class="segmented" id="brush-group" role="group" aria-label="Brush size">
<button type="button" class="segbtn" data-brush="1" aria-pressed="true">1&times;</button>
<button type="button" class="segbtn" data-brush="2" aria-pressed="false">2&times;</button>
<button type="button" class="segbtn" data-brush="3" aria-pressed="false">3&times;</button>
</div>
</fieldset>
</section>
<details class="zonecontrols" id="zone-secondary" open>
<summary>More controls &mdash; sensitivity, zoom &amp; bulk</summary>
<fieldset class="zgroup">
<legend>Live picture</legend>
<label class="ztoggle"><input type="checkbox" id="motion-highlight"> Show armed areas on the live picture</label>
</fieldset>
<fieldset class="zgroup">
<legend>Sensitivity</legend>
<div class="segmented" id="sens-group" role="group" aria-label="Sensitivity"></div>
</fieldset>
<fieldset class="zgroup">
<legend>View</legend>
<div class="segmented" id="mode-group" role="group" aria-label="Pointer mode">
<button type="button" class="segbtn" data-mode="paint" aria-pressed="true">Paint</button>
<button type="button" class="segbtn" data-mode="pan" aria-pressed="false">Pan</button>
</div>
<div class="zoomrow">
<button type="button" class="copybtn" id="zoom-out" aria-label="Zoom out">&minus;</button>
<button type="button" class="copybtn" id="zoom-fit">Fit</button>
<button type="button" class="copybtn" id="zoom-in" aria-label="Zoom in">&plus;</button>
<span class="muted" id="zoom-readout">Fit</span>
</div>
</fieldset>
<fieldset class="zgroup">
<legend>Bulk</legend>
<div class="bulkrow">
<button type="button" class="copybtn" id="arm-all">Arm all</button>
<button type="button" class="copybtn" id="mask-all">Mask all</button>
<button type="button" class="copybtn" id="invert-all">Invert</button>
<button type="button" class="copybtn" id="undo" disabled>Undo</button>
<button type="button" class="copybtn" id="redo" disabled>Redo</button>
<button type="button" class="copybtn" id="toggle-photo">Hide photo</button>
</div>
</fieldset>
<p class="honest">Keys: a / m tool &middot; i invert &middot; + / &minus; / 0 zoom &middot; Ctrl+Z / Ctrl+Shift+Z undo/redo &middot; arrows + Space on the grid.</p>
</details>
<p class="muted warnnote" id="motion-warn" hidden></p>
<p class="zoneprimary">
<button type="button" id="motion-apply" class="searchbtn">Apply to camera</button>
<button type="button" id="motion-discard" class="copybtn" hidden>Discard changes</button>
</p>
<section class="zonecoarse">
<h2>Quick zones</h2>
<div class="zonecoarsemap" id="zone-coarse" role="group" aria-label="Quick zones, 2 columns by 6 rows"></div>
<p id="zone-summary" class="muted" aria-live="polite"></p>
</section>
<div id="motion-endnote" class="endnote" hidden></div>
""")
