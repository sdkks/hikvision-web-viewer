/* hikvision-web-viewer client: HLS via hls.js (server transcodes HEVC -> H.264). */
(function () {
  "use strict";

  var DEBUG = new URLSearchParams(location.search).has("debug");
  function vlog() {
    if (DEBUG) console.log.apply(console, ["[viewer]"].concat([].slice.call(arguments)));
  }

  var current = { hls: null, video: null, timer: null, attempts: 0, bufferedOnce: false, t0: null };
  var renderStatus = function () {};
  var pageKind = null;          // "live" | "play"
  var pageChannel = "101";

  function stopServerSession() {
    const stopUrl = pageKind === "play" ? "/hls-play-stop" : "/hls-live-stop?ch=" + pageChannel;
    try { navigator.sendBeacon(stopUrl); } catch (e) {
      fetch(stopUrl, { method: "POST", keepalive: true }).catch(function () {});
    }
  }

  document.addEventListener("visibilitychange", function () {
    if (!pageKind) return;
    const v = document.getElementById(pageKind === "play" ? "clip-video" : "live-video");
    if (!v) return;
    if (document.hidden) {
      // nobody can see it: stop serving this session entirely
      destroyStream();
      setStatus("warn", "Paused — tab in background. Return to resume.");
      stopServerSession();
      v.dataset.resumePending = "1";
    } else if (v.dataset.resumePending === "1") {
      delete v.dataset.resumePending;
      restartCurrent(v);
    }
  });

  window.addEventListener("pagehide", stopServerSession);

  function restartCurrent(video) {
    if (pageKind === "play") {
      initPlayback(video);
    } else {
      initLive(video);
    }
  }

  function setStatus(kind, text) { renderStatus(kind, text); }

  function destroyStream() {
    if (current.timer) { clearTimeout(current.timer); current.timer = null; }
    if (current.hls) { current.hls.destroy(); current.hls = null; }
    else if (current.video) {
      current.video.removeAttribute("src");
      try { current.video.load(); } catch (e) { /* noop */ }
    }
    current.bufferedOnce = false;
    current.attempts = 0;
  }

  function showEnd(warn, text) {
    var note = document.getElementById("endnote");
    if (!note) return;
    note.className = "endnote" + (warn ? " warn" : "");
    note.innerHTML = "<p>" + text + "</p><div class='row'>" +
      "<button type='button' class='retry' onclick='window.location.reload()'>Replay</button>" +
      "<a class='retry' style='display:inline-flex;align-items:center;text-decoration:none' " +
      "href='/recordings'>← Results</a></div>";
    note.hidden = false;
  }

  /* ── HLS attach: hls.js everywhere; Safari native HLS as fallback ── */

  function attach(video, url, opts) {
    var expected = Number(video.dataset.expected || 0);
    var t0 = null;
    var endedHandled = false;

    function fmt(s) {
      s = Math.max(0, Math.floor(s));
      var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
      return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(r).padStart(2, "0");
    }

    function drawProgress() {
      if (opts.kind !== "play") return;
      var el = document.getElementById("counter");
      var pr = document.getElementById("progress");
      var elapsed = video.currentTime || 0;
      if (el) el.textContent = fmt(elapsed) + " / " + fmt(expected);
      if (pr && expected) pr.style.width = Math.min(100, (elapsed / expected) * 100) + "%";
      video.dataset.elapsed = String(Math.floor(elapsed));
    }

    video.addEventListener("playing", function () {
      setStatus("ok", opts.live ? "Live" : "Playing");
      if (opts.kind === "play" && !t0) t0 = Date.now();
    });
    video.addEventListener("waiting", function () {
      setStatus("connect", opts.live ? "Buffering…" : undefined);
    });
    if (opts.kind === "play") {
      video.addEventListener("timeupdate", drawProgress);
      video.addEventListener("ended", function () {
        var elapsed = Number(video.dataset.elapsed || 0);
        if (expected && elapsed < expected * 0.9) {
          showEnd(true, "Stream ended early — another playback session may have started (the camera allows one).");
        } else {
          showEnd(false, "Clip finished.");
        }
      });
    }

    function goNative() {
      setStatus("connect", "Connecting…");
      video.src = url;
      video.play().catch(function () { /* controls remain */ });
      video.addEventListener("error", function () {
        setStatus("err", "Stream error. Still images below always work.");
      });
    }

    if (window.Hls && Hls.isSupported()) {
      var hls = new Hls({
        lowLatencyMode: !!opts.live,
        backBufferLength: opts.live ? 30 : Infinity,
        maxBufferLength: opts.live ? 12 : 30,
        liveSyncDurationCount: opts.live ? 3 : 0,
      });
      current.hls = hls;
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, function () {
        setStatus("connect", opts.live ? "Connecting…" : "Connecting to clip… starts at its beginning.");
        video.play().catch(function () { /* Low Power Mode etc; controls remain */ });
      });
      hls.on(Hls.Events.FRAG_BUFFERED, function () {
        if (!current.bufferedOnce) {
          current.bufferedOnce = true;
          setStatus("ok", opts.live ? "Live" : "Playing");
        }
      });
      if (opts.live) {
        // Honest live latency: camera GOPs vary, so measure instead of promising.
        var lastShown = -1;
        hls.on(Hls.Events.LEVEL_UPDATED, function () {
          var lat = Math.round(hls.latency || 0);
          if (lat > 0 && lat !== lastShown) {
            lastShown = lat;
            setStatus("ok", "Live · ≈" + lat + " s behind");
          }
        });
      }
      hls.on(Hls.Events.ERROR, function (evt, data) {
        if (!data.fatal) return;
        vlog("fatal", data.type, data.details);
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          if (opts.live) {
            current.attempts += 1;
            var delays = [2000, 4000, 8000, 15000, 30000];
            var delay = delays[Math.min(current.attempts - 1, delays.length - 1)];
            setStatus("warn", "Connection lost — reconnecting (attempt " + current.attempts + ")…");
            current.timer = setTimeout(function () { hls.startLoad(); }, delay);
          } else {
            setStatus("err", "Connection lost. The camera's streams usually recover — try Replay.");
          }
        } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          setStatus("warn", "Recovering playback…");
          hls.recoverMediaError();
        } else {
          setStatus("err", "Stream error: " + data.details + ". Still images below always work.");
        }
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      goNative(); // Safari: plays the (H.264) HLS natively
    } else {
      setStatus("nosupport", "This browser can't play the stream. Use the still-image links below.");
    }
  }

  /* ── live page ────────────────────────────────────────────────── */

  function initLive(video) {
    pageKind = "live";
    pageChannel = video.dataset.channel || "101";
    renderStatus = function (kind, text) {
      var line = document.getElementById("status-line");
      if (!line) return;
      var dot = line.querySelector(".dot");
      var label = line.querySelector(".label");
      var map = {
        ok: ["dot ok", text || "Live"],
        connect: ["dot spin", text || "Connecting…"],
        warn: ["dot warn", text],
        err: ["dot err", text],
        nosupport: ["dot warn", text],
      };
      var m = map[kind] || ["dot", text];
      if (dot) dot.className = "dot " + m[0].replace("dot ", "");
      if (label) label.textContent = m[1];
      var retry = document.getElementById("retry");
      if (retry) retry.hidden = !(kind === "err");
    };

    function start(channel) {
      destroyStream();
      video.dataset.channel = channel;
      video.poster = "/snapshot?ch=" + channel;
      renderStatus("connect", "Connecting…");
      attach(video, "/hls/live/index.m3u8?ch=" + channel, { live: true, kind: "live" });
    }

    document.querySelectorAll(".chan").forEach(function (btn) {
      btn.addEventListener("click", function () {
        document.querySelectorAll(".chan").forEach(function (b) {
          b.setAttribute("aria-pressed", b === btn ? "true" : "false");
        });
        renderStatus("connect", "Switching…");
        start(btn.dataset.channel);
      });
    });

    var retryBtn = document.getElementById("retry");
    if (retryBtn) retryBtn.addEventListener("click", function () {
      renderStatus("connect", "Connecting…");
      start(video.dataset.channel || "101");
    });

    start(video.dataset.channel || "101");
  }

  /* ── playback page ────────────────────────────────────────────── */

  function initPlayback(video) {
    pageKind = "play";
    pageChannel = video.dataset.channel || "101";
    renderStatus = function (kind, text) {
      var line = document.getElementById("status-line");
      if (!line) return;
      var dot = line.querySelector(".dot");
      var label = line.querySelector(".label");
      var map = {
        ok: ["dot ok", text || "Playing"],
        connect: ["dot spin", text || "Connecting…"],
        warn: ["dot warn", text],
        err: ["dot err", text],
        nosupport: ["dot warn", text],
      };
      var m = map[kind] || ["dot", text];
      if (dot) dot.className = "dot " + m[0].replace("dot ", "");
      if (label) label.textContent = m[1];
    };
    attach(video, video.dataset.url, { live: false, kind: "play" });
  }

  /* ── boot ─────────────────────────────────────────────────────── */

  document.addEventListener("DOMContentLoaded", function () {
    var live = document.getElementById("live-video");
    var clip = document.getElementById("clip-video");
    if (live) initLive(live);
    if (clip) initPlayback(clip);

    document.querySelectorAll("form.search").forEach(function (form) {
      form.addEventListener("submit", function () {
        var btn = form.querySelector(".searchbtn");
        if (btn) { btn.disabled = true; btn.textContent = "Searching the camera…"; }
      });
    });

    document.querySelectorAll(".copybtn").forEach(function (btn) {
      btn.setAttribute("aria-live", "polite");
      btn.addEventListener("click", function () {
        var target = document.querySelector(btn.dataset.target);
        if (target) navigator.clipboard && navigator.clipboard.writeText(target.textContent.trim());
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = "Copy"; }, 1500);
      });
    });
  });

  window.__initLive = initLive;
  window.__initPlayback = initPlayback;
})();
