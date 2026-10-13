/* hikvision-web-viewer client: HLS via hls.js (server transcodes HEVC -> H.264). */
(function () {
  "use strict";

  var DEBUG = new URLSearchParams(location.search).has("debug");
  function vlog() {
    if (DEBUG) console.log.apply(console, ["[viewer]"].concat([].slice.call(arguments)));
  }

  var current = { hls: null, video: null, timer: null, attempts: 0, bufferedOnce: false, t0: null };
  var renderStatus = function () {};
  var gestureFallbackArmed = false;

  /* Start playback without a click. Browsers gate autoplay on muted media, and
     Firefox additionally refuses until the element is demonstrably muted, so we
     set the properties (not just attributes) and retry when data arrives. If the
     policy still refuses, the first tap/click/keypress anywhere starts it. */
  function tryPlay(video) {
    if (!video) return;
    video.muted = true;
    video.defaultMuted = true;
    video.autoplay = true;
    video.setAttribute("playsinline", "");
    var attempt = video.play();
    if (attempt && typeof attempt.catch === "function") {
      attempt.catch(function (err) {
        if (err && err.name === "NotAllowedError") {
          // Browser policy: only a real gesture can start playback here.
          showTapStart(video);
        }
        if (!gestureFallbackArmed) {
          gestureFallbackArmed = true;
          var resume = function () {
            document.removeEventListener("pointerdown", resume);
            document.removeEventListener("keydown", resume);
            document.removeEventListener("touchstart", resume);
            tryPlay(current.video || video);
          };
          document.addEventListener("pointerdown", resume, { passive: true });
          document.addEventListener("keydown", resume);
          document.addEventListener("touchstart", resume, { passive: true });
        }
      });
    }
  }

  function browserName() {
    var ua = navigator.userAgent;
    if (navigator.brave) return "Brave";
    if (/Edg\//.test(ua)) return "Edge";
    if (/OPR\//.test(ua)) return "Opera";
    if (/FxiOS/.test(ua)) return "Firefox on iOS";
    if (/Firefox\//.test(ua)) return "Firefox";
    if (/CriOS/.test(ua)) return "Chrome on iOS";
    if (/Chrome\//.test(ua)) return "Chrome";
    if (/Safari\//.test(ua)) return "Safari";
    return "This browser";
  }

  function tapStartButton() {
    return document.getElementById("tap-start");
  }

  function showTapStart(video) {
    var btn = tapStartButton();
    if (!btn) return;
    var label = btn.dataset.label || "view";
    var name = browserName();
    btn.innerHTML =
      name + " doesn&rsquo;t permit autoplay in this tab. " +
      '<span class="act">Click or tap to play</span>';
    btn.setAttribute(
      "aria-label",
      name + " does not permit autoplay in this tab. Activate to play the " + label + "."
    );
    btn.hidden = false;
    btn.onclick = function () {
      btn.hidden = true;
      tryPlay(video);
    };
  }

  function hideTapStart() {
    var btn = tapStartButton();
    if (btn) btn.hidden = true;
  }
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
    maybeAutoNext(note);
  }

  /* Optional chained playback: only when the viewer opted in, only when a next
     recording exists, always visible and cancellable. */
  var autoNextTimer = null;

  function autoNextEnabled() {
    try { return localStorage.getItem("autoNext") === "1"; } catch (e) { return false; }
  }

  function maybeAutoNext(note) {
    if (!autoNextEnabled()) return;
    var link = document.querySelector("#later a");
    if (!link) return;
    var seconds = 3;
    var row = document.createElement("div");
    row.className = "row";
    note.append(row);
    var render = function () {
      var when = link.querySelector(".when").textContent.replace(/^[\u25c0\u25b6]\s*/, "");
      row.innerHTML = "<a class='retry' href='" + link.getAttribute("href") + "'>Next in " +
        seconds + "\u2026 \u25b6 " + when + "</a>";
    };
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "copybtn";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", function () {
      if (autoNextTimer) { clearInterval(autoNextTimer); autoNextTimer = null; }
      row.textContent = "Auto-advance cancelled.";
      cancel.remove();
    });
    render();
    note.append(cancel);
    autoNextTimer = setInterval(function () {
      seconds -= 1;
      if (seconds <= 0) {
        clearInterval(autoNextTimer);
        autoNextTimer = null;
        window.location.href = link.getAttribute("href");
        return;
      }
      render();
    }, 1000);
  }

  /* Neighbour table under the player, with honest gap labels. */
  function initNeighbours() {
    var box = document.getElementById("jumps");
    if (!box) return;

    var check = document.getElementById("auto-next");
    if (check) {
      check.checked = autoNextEnabled();
      check.addEventListener("change", function () {
        try { localStorage.setItem("autoNext", check.checked ? "1" : "0"); } catch (e) { /* noop */ }
      });
    }

    var params = new URLSearchParams(location.search);
    var from = params.get("from");
    var to = params.get("to");
    if (!from || !to) return;

    function load(attempt) {
      fetch("/api/neighbours?from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to),
            { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          box.hidden = false;
          if (data.state !== "ok") {
            // The camera's search is known to be flaky: name the reason, retry once,
            // and always leave a way to ask again.
            if (attempt === 0) { window.setTimeout(function () { load(1); }, 1500); return; }
            showJumpProblem("Can\u2019t list other recordings (" +
              (data.state_detail || data.state) + ").");
            return;
          }
          renderJumpList("earlier", data.earlier, data.more_earlier, "earlier");
          renderJumpList("later", data.later, data.more_later, "later");
        })
        .catch(function (err) {
          box.hidden = false;
          if (attempt === 0) { window.setTimeout(function () { load(1); }, 1500); return; }
          showJumpProblem("Couldn\u2019t load nearby recordings (" + (err && err.message ? err.message : err) + ").");
        });
    }

    function showJumpProblem(message) {
      var earlier = document.getElementById("earlier");
      earlier.innerHTML = "<li class='none'>" + message + "</li>";
      document.getElementById("later").innerHTML = "";
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "copybtn";
      btn.textContent = "Retry";
      btn.addEventListener("click", function () { load(1); });
      li.append(btn);
      earlier.append(li);
    }

    load(0);
  }

  function gapText(seconds) {
    if (seconds < 60) return "";
    var minutes = Math.round(seconds / 60);
    return minutes >= 60
      ? Math.floor(minutes / 60) + " h " + String(minutes % 60).padStart(2, "0") + " min gap"
      : minutes + " min gap";
  }

  function renderJumpList(id, items, more, direction) {
    var list = document.getElementById(id);
    list.textContent = "";
    if (!items.length) {
      var none = document.createElement("li");
      none.className = "none";
      none.textContent = direction === "earlier"
        ? "No earlier recordings today."
        : "No later recordings today.";
      list.append(none);
      return;
    }
    for (var i = 0; i < items.length; i++) {
      var f = items[i];
      var gap = gapText(f.gap_s);
      var li = document.createElement("li");
      li.innerHTML =
        "<a href='" + playHref(f.start_epoch, f.end_epoch, f.size) + "'>" +
        "<span class='when'>" + (direction === "earlier" ? "\u25c0 " : "\u25b6 ") +
        f.start_local + "</span>" +
        "<span class='meta'>" + humanDuration(f.dur_s) + " \u00b7 " + humanSize(f.size) + "</span>" +
        (gap ? "<span class='gap'>" + gap + "</span>" : "") +
        "</a>";
      list.append(li);
    }
    if (more) {
      var moreLi = document.createElement("li");
      moreLi.className = "none";
      moreLi.textContent = "\u2026more " + direction + " recordings today (use Browse by time).";
      list.append(moreLi);
    }
  }

  /* ── HLS attach: hls.js everywhere; Safari native HLS as fallback ── */

  function attach(video, url, opts) {
    var expected = Number(video.dataset.expected || 0);
    var t0 = null;
    var endedHandled = false;
    var playedOnce = false; // distinguishes "autoplay blocked" from "user paused"

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

    // Media events keep retrying: the first call often precedes usable data.
    video.addEventListener("loadedmetadata", function () { tryPlay(video); });
    video.addEventListener("canplay", function () { tryPlay(video); });
    tryPlay(video);

    video.addEventListener("playing", function () {
      playedOnce = true;
      // A stale "playing" from the previous channel can fire during teardown;
      // only clear the prompt when playback is genuinely running.
      if (!video.paused && video.readyState >= 2) hideTapStart();
      setStatus("ok", opts.live ? "Live" : "Playing");
      if (opts.kind === "play" && !t0) t0 = Date.now();
    });
    video.addEventListener("pause", function () {
      // Prompt only when playback never started (browser policy blocked it).
      // A deliberate pause after watching is the user's business.
      window.setTimeout(function () {
        if (!playedOnce && video.paused && video.readyState >= 2 && !video.ended) {
          showTapStart(video);
        }
      }, 3000);
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
      tryPlay(video);
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
        tryPlay(video);
      });
      hls.on(Hls.Events.FRAG_BUFFERED, function () {
        if (!current.bufferedOnce) {
          current.bufferedOnce = true;
          setStatus("ok", opts.live ? "Live" : "Playing");
        }
        tryPlay(video); // data is buffered now — the most likely moment to succeed
        window.setTimeout(function () {
          if (!playedOnce && video.paused && video.readyState >= 2) showTapStart(video);
        }, 1200);
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
    initNeighbours();
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

  /* ── time picker (recordings page) ──────────────────────────────
     Availability-driven drill-down: day -> hour tiles -> minute scrubber -> play.
     One camera search per day (cached); states mirror the server's honesty enum. */

  function initPicker() {
    const dateInput = document.getElementById("pick-date");
    const grid = document.getElementById("hour-grid");
    if (!dateInput || !grid) return;

    const chipsBox = document.getElementById("recent-days");
    const statusLine = document.getElementById("picker-status");
    const dayView = document.getElementById("day-view");
    const dayHeading = document.getElementById("day-heading");
    const hourView = document.getElementById("hour-view");
    const hourHeading = document.getElementById("hour-heading");
    const readout = document.getElementById("scrub-readout");
    const scrubber = document.getElementById("scrubber");
    const playBar = document.getElementById("play-bar");
    const playFrom = document.getElementById("play-from");

    const cache = new Map();
    const state = { date: null, hour: null, minute: null, coverage: null };
    const pad = (n) => String(n).padStart(2, "0");
    const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const hhmm = (epoch) => {
      const d = new Date(epoch * 1000);
      return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    const dayLabel = (iso) =>
      new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, {
        weekday: "short", day: "numeric", month: "short", year: "numeric",
      });

    function setStatus(kind, text) {
      const dot = statusLine.querySelector(".dot");
      const label = statusLine.querySelector(".label");
      if (dot) dot.className = "dot" + (kind ? " " + kind : "");
      if (label) label.textContent = text || "";
    }

    function recentDays() {
      const out = [];
      for (let i = 0; i < 7; i++) {
        const d = new Date();
        d.setDate(d.getDate() - i);
        out.push({ iso: isoDate(d), label: i === 0 ? "Today" : i === 1 ? "Yesterday"
          : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" }) });
      }
      return out;
    }

    function renderChips() {
      chipsBox.textContent = "";
      for (const day of recentDays()) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.textContent = day.label;
        chip.setAttribute("aria-pressed", String(day.iso === state.date));
        chip.addEventListener("click", () => selectDate(day.iso, { push: true }));
        chipsBox.append(chip);
      }
    }

    function syncUrl(push) {
      const params = new URLSearchParams(location.search);
      for (const key of ["date", "hour", "min"]) params.delete(key);
      if (state.date) params.set("date", state.date);
      if (state.hour !== null) params.set("hour", String(state.hour));
      if (state.minute !== null && state.hour !== null) params.set("min", String(state.minute));
      const url = `${location.pathname}?${params.toString()}`;
      history[push ? "pushState" : "replaceState"](null, "", url);
    }

    function selectDate(iso, opts) {
      state.date = iso;
      state.hour = null;
      // A deep link may carry a minute; otherwise start clean.
      state.minute = opts && opts.minute !== undefined ? opts.minute : null;
      dateInput.value = iso;
      renderChips();
      hourView.hidden = true;
      playBar.hidden = true;
      loadDay(iso, opts);
    }

    async function loadDay(iso, opts) {
      dayView.hidden = false;
      if (cache.has(iso)) {
        renderDay(cache.get(iso));
      } else {
        setStatus("spin", `Checking ${dayLabel(iso)}…`);
        grid.textContent = "";
        try {
          const res = await fetch(`/api/coverage?date=${iso}`, { cache: "no-store" });
          const payload = await res.json();
          if (["ok", "empty"].includes(payload.state)) cache.set(iso, payload);
          state.coverage = payload;
          renderDay(payload);
        } catch (err) {
          state.coverage = null;
          grid.textContent = "";
          setStatus("err", `Couldn't reach the viewer: ${err}`);
          return;
        }
      }
      if (opts && opts.push) syncUrl(true);
      else syncUrl(false);
      if (opts && opts.focusHour !== undefined) {
        selectHour(opts.focusHour, { push: !!opts.push, minute: opts.minute });
      }
    }

    function coverageUnknown(payload) {
      return payload && ["service_down", "unreachable", "bad_response"].includes(payload.state);
    }

    function renderDay(payload) {
      grid.textContent = "";
      // Count each recording once: per-hour counts would double a clip that
      // crosses an hour boundary.
      const clips = (payload.ranges || []).reduce((total, r) => total + r.clips, 0);
      const unknown = coverageUnknown(payload);
      dayHeading.textContent = unknown
        ? dayLabel(payload.date)
        : `${dayLabel(payload.date)} · ${clips} clip${clips === 1 ? "" : "s"}`;

      const retryBtn = document.getElementById("picker-retry");
      if (unknown) {
        const messages = {
          service_down: "The camera's search service isn't responding (known to be flaky). Live view and clip playback still work.",
          unreachable: "Camera unreachable.",
          bad_response: "Unexpected search reply (possible firmware drift). Live view still works.",
        };
        const detail = payload.state_detail
          ? ` (${String(payload.state_detail).slice(0, 120)})`
          : "";
        setStatus("warn", (messages[payload.state] || "Couldn't check this day.") + detail);
        if (retryBtn) {
          retryBtn.hidden = false;
          retryBtn.textContent = "Retry this day";
        }
      } else if (payload.state === "empty") {
        setStatus("", `No recordings on ${dayLabel(payload.date)}.`);
      } else if (payload.state === "truncated") {
        setStatus("warn", "This day has more clips than one query returns — coverage may be incomplete.");
      } else {
        setStatus("ok", `Checked ${dayLabel(payload.date)}.`);
        if (retryBtn) retryBtn.hidden = true;
      }
      const idleHint = document.getElementById("idle-hint");
      if (idleHint) idleHint.hidden = true;

      renderFiles(
        "day-files",
        coverageUnknown(payload) ? [] : filesInDay(),
        coverageUnknown(payload)
          ? "Files can't be listed for this day (see above)."
          : "No recording files on this day."
      );

      const now = new Date();
      const isToday = isoDate(now) === payload.date;
      for (let hour = 0; hour < 24; hour++) {
        const level = payload.hours[hour];
        const tile = document.createElement("button");
        tile.type = "button";
        tile.className = `hour-tile lvl-${level}${unknown ? " unknown" : ""}`;
        if (isToday && now.getHours() === hour) tile.classList.add("now");
        tile.dataset.hour = String(hour);
        tile.setAttribute("aria-pressed", "false");
        const count = payload.hour_clips[hour];
        tile.setAttribute(
          "aria-label",
          `${pad(hour)}:00, ${count} clip${count === 1 ? "" : "s"}, ` +
            (unknown ? "couldn't check" : level === 0 ? "no recording" : "recorded")
        );
        tile.innerHTML =
          `<span class="h">${pad(hour)}</span>` +
          `<span class="n">${count ? "·" + count : ""}</span>` +
          `<span class="meter" aria-hidden="true"><span></span></span>`;
        if (!unknown) {
          tile.addEventListener("click", () => selectHour(hour, { push: true }));
        } else {
          tile.disabled = true;
        }
        grid.append(tile);
      }
    }

    function selectHour(hour, opts) {
      if (!state.coverage) return;
      const keepMinute = opts && opts.minute !== undefined ? opts.minute : null;
      state.hour = hour;
      state.minute = keepMinute;
      hourView.hidden = false;
      const iso = state.coverage.date;
      hourHeading.textContent = `${dayLabel(iso).replace(/ \d{4}$/, "")} · ${pad(hour)}:00`;
      for (const tile of grid.querySelectorAll(".hour-tile")) {
        tile.setAttribute("aria-pressed", String(Number(tile.dataset.hour) === hour));
      }
      renderHour();
      if (opts && opts.push) syncUrl(true);
      hourHeading.focus({ preventScroll: true });
    }

    function renderHour() {
      const hour = state.hour;
      const minutes = state.coverage.minutes[hour];
      const covered = minutes.map((m, i) => (m > 0 ? i : -1)).filter((i) => i >= 0);

      // minute strip: covered minutes filled, empty minutes paper
      const cells = [];
      for (let i = 0; i < 60; i++) {
        const on = minutes[i] > 0;
        cells.push(`${on ? "var(--cov-4)" : "var(--inset)"} ${(i / 60) * 100}% ${((i + 1) / 60) * 100}%`);
      }
      scrubber.style.setProperty("--track", `linear-gradient(to right, ${cells.join(",")})`);
      scrubber.disabled = covered.length === 0;

      if (covered.length === 0) {
        readout.textContent = `Nothing recorded in ${pad(hour)}:00–${pad(hour)}:59.`;
        scrubber.value = "0";
        playBar.hidden = true;
        const emptyNote = document.getElementById("play-note");
        if (emptyNote) emptyNote.hidden = true;
        renderFiles("hour-files", [], "No recording files in this hour.");
        return;
      }
      const initial = state.minute !== null && minutes[state.minute] > 0 ? state.minute : covered[0];
      scrubber.value = String(initial);
      updateMinute(initial, { push: false });

      renderFiles("hour-files", filesInHour(hour), "No recording files in this hour.");
    }

    function fileRow(file) {
      const row = document.createElement("div");
      row.className = "file-row";
      row.innerHTML =
        `<span class="when">${hhmm(file.start_epoch)} → ${hhmm(file.end_epoch)}</span>` +
        `<span class="meta">${humanDuration(file.dur_s)} · ${humanSize(file.size)}</span>`;
      const acts = document.createElement("span");
      acts.className = "acts";
      const play = document.createElement("a");
      play.className = "playbtn";
      play.textContent = "▶ Play";
      play.href = playHref(file.start_epoch, file.end_epoch, file.size);
      const dl = document.createElement("a");
      dl.className = "dlbtn";
      dl.textContent = "⬇ Download";
      // By-name download targets this exact file on the card.
      dl.href =
        `/download?from=${camTime(file.start_epoch)}&to=${camTime(file.end_epoch)}` +
        (file.name ? `&name=${encodeURIComponent(file.name)}` : "") +
        `&size=${file.size}`;
      acts.append(play, dl);
      row.append(acts);
      return row;
    }

    function renderFiles(containerId, files, emptyText) {
      const box = document.getElementById(containerId);
      if (!box) return;
      box.textContent = "";
      if (!files.length) {
        const p = document.createElement("p");
        p.className = "empty";
        p.textContent = emptyText;
        box.append(p);
        return;
      }
      for (const file of files) box.append(fileRow(file));
    }

    function filesInDay() {
      return state.coverage.files || [];
    }

    function filesInHour(hour) {
      const dayStart = new Date(`${state.coverage.date}T00:00:00`).getTime() / 1000;
      const from = dayStart + hour * 3600;
      const to = from + 3600;
      return filesInDay().filter((f) => f.start_epoch < to && f.end_epoch > from);
    }




    function containingRange(epoch) {
      // Overlap by minute: the selected minute is covered when any part of it
      // falls inside the recording (ranges start/end at second precision).
      return (state.coverage.ranges || []).find(
        (r) => r.start_epoch < epoch + 60 && r.end_epoch > epoch
      );
    }

    function playStart(epoch, range) {
      // Never ask the camera for a time before the recording begins.
      return Math.max(epoch, range.start_epoch);
    }

    function snapToCovered(minute) {
      const minutes = state.coverage.minutes[state.hour];
      if (minutes[minute] > 0) return minute;
      for (let d = 1; d < 60; d++) {
        if (minute + d < 60 && minutes[minute + d] > 0) return minute + d;
        if (minute - d >= 0 && minutes[minute - d] > 0) return minute - d;
      }
      return minute;
    }

    function updateMinute(minute, opts) {
      state.minute = minute;
      const minutes = state.coverage.minutes[state.hour];
      const range = containingRange(epochOf(minute));
      const covered = minutes[minute] > 0;
      const from = range ? playStart(epochOf(minute), range) : 0;
      readout.textContent = range
        ? `${pad(state.hour)}:${pad(minute)} · recording (` +
          humanDuration(range.end_epoch - from) + ` from ${hhmm(epochOf(minute))})`
        : `${pad(state.hour)}:${pad(minute)} · no recording`;
      scrubber.setAttribute(
        "aria-valuetext",
        `${pad(state.hour)}:${pad(minute)}, ${covered ? "recording" : "no recording"}`
      );
      const playNote = document.getElementById("play-note");
      if (range) {
        playBar.hidden = false;
        if (playNote) playNote.hidden = false;
        playFrom.href = playHref(from, range.end_epoch, range.size);
        playFrom.textContent = `▶ Play from ${hhmm(from)} (${humanDuration(range.end_epoch - from)}, plays in real time)`;
      } else {
        playBar.hidden = true;
        if (playNote) playNote.hidden = true;
      }
      if (opts && opts.push) syncUrl(true);
      else syncUrl(false);
    }

    function epochOf(minute) {
      const dayStart = new Date(`${state.coverage.date}T00:00:00`).getTime() / 1000;
      return dayStart + state.hour * 3600 + minute * 60;
    }

    const retryControl = document.getElementById("picker-retry");
    if (retryControl) {
      retryControl.addEventListener("click", () => {
        if (!state.date) return;
        cache.delete(state.date); // failed days are never cached, but be explicit
        loadDay(state.date, { push: true });
      });
    }

    dateInput.addEventListener("change", () => {
      if (dateInput.value) selectDate(dateInput.value, { push: true });
    });
    scrubber.addEventListener("input", () => {
      const snapped = snapToCovered(Number(scrubber.value));
      scrubber.value = String(snapped);
      updateMinute(snapped, { push: false });
    });
    scrubber.addEventListener("change", () => {
      const snapped = snapToCovered(Number(scrubber.value));
      updateMinute(snapped, { push: true });
    });
    window.addEventListener("popstate", () => applyUrl({ focus: false }));

    function applyUrl(opts) {
      const params = new URLSearchParams(location.search);
      const date = params.get("date");
      if (!date) return;
      const hourRaw = params.get("hour");
      const minRaw = params.get("min");
      const hour = hourRaw === null ? null : Number(hourRaw);
      const minute = minRaw === null ? undefined : Number(minRaw);
      selectDate(date, {
        push: false,
        minute: minute,
        focusHour: hour === null ? undefined : hour,
      });
      void opts;
    }

    dateInput.value = isoDate(new Date());
    renderChips();
    applyUrl({ focus: false });
  }

  /* ── motion zone editor (Motion tab) ─────────────────────────────
     One mutable state (the camera's 18x22 armed/masked grid) with two honest
     presentations: a paint canvas over the live still, and 12 whole-cell quick
     zones. Apply always reports the camera's readback, never the request. */

  function initMotion() {
    var grid = document.getElementById("zone-grid");
    if (!grid) return;

    var ROWS = 18, COLS = 22;
    var SENS = [0, 20, 40, 60, 80, 100];
    var ZONE_ROWS = 6, ZONE_COLS = 2;
    var ZONE_H = ROWS / ZONE_ROWS, ZONE_W = COLS / ZONE_COLS;

    var canvas = document.getElementById("zone-canvas");
    var viewport = document.getElementById("zone-viewport");
    var statusBox = document.getElementById("motion-status");
    var dirtyBar = document.getElementById("motion-dirty");
    var dirtyText = document.getElementById("motion-dirty-text");
    var endnote = document.getElementById("motion-endnote");
    var errorBox = document.getElementById("motion-error");
    var summaryEl = document.getElementById("zone-summary");
    var sensGroup = document.getElementById("sens-group");
    var offNote = document.getElementById("motion-off-note");
    var card = document.querySelector(".zonecard");

    var state = {
      ready: false, camera: null, draft: null, tool: "arm", brush: 1, mode: "paint",
      zoom: 1, fit: true, cursor: { r: 0, c: 0 }, stroke: null,
      pointers: {}, pinch: null, undo: [], redo: [], applying: false
    };
    var cellEls = [];

    function setStatus(kind, text) {
      var dot = statusBox.querySelector(".dot");
      var label = statusBox.querySelector(".label");
      if (dot) dot.className = "dot" + (kind ? " " + kind : "");
      if (label) label.textContent = text || "";
    }

    /* ── state helpers ── */
    function cloneCells(cells) {
      var out = [];
      for (var r = 0; r < ROWS; r++) out.push(cells[r].slice());
      return out;
    }
    function cloneState(from) {
      return {
        enabled: !!from.enabled, enable_highlight: !!from.enable_highlight,
        sensitivity: from.sensitivity, cells: cloneCells(from.cells)
      };
    }
    function fromPayload(data) {
      return {
        enabled: !!data.enabled, enable_highlight: !!data.enable_highlight,
        sensitivity: data.sensitivity, cells: cloneCells(data.cells)
      };
    }
    function cellsEqual(a, b) {
      for (var r = 0; r < ROWS; r++) for (var c = 0; c < COLS; c++) {
        if (!!a[r][c] !== !!b[r][c]) return false;
      }
      return true;
    }
    function isDirty() {
      if (!state.camera || !state.draft) return false;
      var a = state.camera, b = state.draft;
      return a.enabled !== b.enabled || a.enable_highlight !== b.enable_highlight ||
        a.sensitivity !== b.sensitivity || !cellsEqual(a.cells, b.cells);
    }
    function countArmed(cells) {
      var n = 0;
      for (var r = 0; r < ROWS; r++) for (var c = 0; c < COLS; c++) if (cells[r][c]) n++;
      return n;
    }
    function summaryText(cells) {
      var n = countArmed(cells);
      return n + " of " + (ROWS * COLS) + " cells armed (" +
        Math.round(100 * n / (ROWS * COLS)) + "%)";
    }
    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, function (ch) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
      });
    }

    /* ── grid DOM ── */
    function buildGrid() {
      grid.textContent = "";
      cellEls = [];
      for (var r = 0; r < ROWS; r++) {
        // A role=row wrapper between grid and gridcell is what ARIA wants;
        // display:contents keeps the 22x18 CSS-grid layout identical.
        var row = document.createElement("div");
        row.className = "zrow";
        row.setAttribute("role", "row");
        var rowEls = [];
        for (var c = 0; c < COLS; c++) {
          var el = document.createElement("div");
          el.id = "zc-" + r + "-" + c;
          el.className = "zcell unknown";
          el.setAttribute("role", "gridcell");
          el.setAttribute("aria-rowindex", String(r + 1));
          el.setAttribute("aria-colindex", String(c + 1));
          el.dataset.r = String(r);
          el.dataset.c = String(c);
          row.appendChild(el);
          rowEls.push(el);
        }
        grid.appendChild(row);
        cellEls.push(rowEls);
      }
    }
    function setReady(ready) {
      state.ready = ready;
      if (card) card.classList.toggle("not-ready", !ready);
    }
    /* Before a successful read (and after a failed one) every cell is honestly
       unknown; nothing is painted masked or armed on the camera's behalf. */
    function renderUnknown() {
      state.camera = null;
      state.draft = null;
      state.stroke = null;
      for (var r = 0; r < ROWS; r++) for (var c = 0; c < COLS; c++) {
        var el = cellEls[r] && cellEls[r][c];
        if (!el) continue;
        el.className = "zcell unknown";
        el.setAttribute("aria-label", "Row " + (r + 1) + ", column " + (c + 1) + ", not read");
      }
      var box = document.getElementById("zone-coarse");
      if (box) box.textContent = "";
      if (summaryEl) summaryEl.textContent = "";
      if (dirtyBar) dirtyBar.hidden = true;
      var d = document.getElementById("motion-discard");
      if (d) d.hidden = true;
      renderWarning();
      setReady(false);
    }
    function paintCellEl(r, c) {
      var el = cellEls[r][c];
      var armed = !!state.draft.cells[r][c];
      var cls = "zcell " + (armed ? "armed" : "masked");
      if (state.cursor.r === r && state.cursor.c === c) cls += " cursor";
      el.className = cls;
      el.setAttribute("aria-label", "Row " + (r + 1) + ", column " + (c + 1) + ", " +
        (armed ? "armed" : "masked"));
    }
    function renderCellsAll() {
      for (var r = 0; r < ROWS; r++) for (var c = 0; c < COLS; c++) paintCellEl(r, c);
      grid.setAttribute("aria-activedescendant", "zc-" + state.cursor.r + "-" + state.cursor.c);
    }
    function moveCursor(r, c) {
      var oldR = state.cursor.r, oldC = state.cursor.c;
      state.cursor = { r: r, c: c };
      if (cellEls.length) {
        paintCellEl(oldR, oldC);
        paintCellEl(r, c);
        grid.setAttribute("aria-activedescendant", "zc-" + r + "-" + c);
      }
    }
    function setCell(r, c, armed) {
      if (!!state.draft.cells[r][c] === armed) return false;
      state.draft.cells[r][c] = armed;
      paintCellEl(r, c);
      return true;
    }

    /* ── painting ── */
    function brushOrigin(r, c) {
      var n = state.brush;
      var off = (n - 1) >> 1; // centre the brush; the pointer cell is its top-left-ish cell
      return {
        r0: Math.min(Math.max(r - off, 0), ROWS - n),
        c0: Math.min(Math.max(c - off, 0), COLS - n)
      };
    }
    function paintBrush(r, c) {
      var o = brushOrigin(r, c), arm = state.tool === "arm", changed = false;
      for (var rr = o.r0; rr < o.r0 + state.brush; rr++) {
        for (var cc = o.c0; cc < o.c0 + state.brush; cc++) {
          if (setCell(rr, cc, arm)) changed = true;
        }
      }
      return changed;
    }
    function lineTo(r0, c0, r1, c1, cb) {
      var dr = Math.abs(r1 - r0), dc = Math.abs(c1 - c0), steps = Math.max(dr, dc);
      if (steps === 0) { cb(r0, c0); return; }
      for (var i = 0; i <= steps; i++) {
        cb(Math.round(r0 + (r1 - r0) * i / steps), Math.round(c0 + (c1 - c0) * i / steps));
      }
    }
    function beginStroke() { return cloneCells(state.draft.cells); }
    function commitStroke(before) {
      if (cellsEqual(before, state.draft.cells)) return;
      state.undo.push(before);
      if (state.undo.length > 60) state.undo.shift();
      state.redo = [];
      afterEdit();
    }

    /* ── render ── */
    function afterEdit() {
      renderCoarse();
      renderSummary();
      renderSensitivity();
      renderEnabled();
      renderWarning();
      updateDirty();
      updateUndoRedo();
    }
    function renderWarning() {
      var el = document.getElementById("motion-warn");
      if (!el) return;
      var show = !!(state.draft && state.draft.enabled && countArmed(state.draft.cells) === 0);
      el.hidden = !show;
      el.textContent = show
        ? "Motion detection is on but no cells are armed \u2014 motion detection would never " +
          "trigger. Arm at least one zone before applying."
        : "";
    }
    function renderAll() {
      renderCellsAll();
      afterEdit();
      applyZoomWidth();
      setTool(state.tool);
    }
    function renderSummary() {
      if (summaryEl && state.draft) summaryEl.textContent = summaryText(state.draft.cells);
    }
    function renderSensitivity() {
      if (!sensGroup || !state.draft) return;
      if (!sensGroup.childElementCount) {
        SENS.forEach(function (v) {
          var b = document.createElement("button");
          b.type = "button";
          b.className = "segbtn";
          b.dataset.sens = String(v);
          b.textContent = String(v);
          b.addEventListener("click", function () {
            if (!state.ready || !state.draft) return;
            state.draft.sensitivity = v;
            afterEdit();
          });
          sensGroup.appendChild(b);
        });
      }
      Array.prototype.forEach.call(sensGroup.children, function (b) {
        b.setAttribute("aria-pressed",
          Number(b.dataset.sens) === state.draft.sensitivity ? "true" : "false");
      });
    }
    function renderEnabled() {
      if (!state.draft) return;
      var en = document.getElementById("motion-enabled");
      var hl = document.getElementById("motion-highlight");
      if (en) en.checked = state.draft.enabled;
      if (hl) hl.checked = state.draft.enable_highlight;
      if (offNote) offNote.hidden = state.draft.enabled;
    }
    function renderCoarse() {
      var box = document.getElementById("zone-coarse");
      if (!box || !state.draft) return;
      if (!box.childElementCount) {
        for (var zr = 0; zr < ZONE_ROWS; zr++) for (var zc = 0; zc < ZONE_COLS; zc++) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "zonebtn";
          btn.dataset.zoneRow = String(zr);
          btn.dataset.zoneCol = String(zc);
          box.appendChild(btn);
        }
      }
      // Quick zones apply the active tool rather than toggling, so they are
      // buttons whose name states both the current state and the action.
      var action = state.tool === "arm" ? "Arm" : "Mask";
      var idx = 0;
      for (var r2 = 0; r2 < ZONE_ROWS; r2++) for (var c2 = 0; c2 < ZONE_COLS; c2++) {
        var b = box.children[idx++];
        var r0 = r2 * ZONE_H, c0 = c2 * ZONE_W, total = ZONE_H * ZONE_W, armed = 0;
        for (var rr = r0; rr < r0 + ZONE_H; rr++)
          for (var cc = c0; cc < c0 + ZONE_W; cc++) if (state.draft.cells[rr][cc]) armed++;
        var st = armed === total ? "true" : armed === 0 ? "false" : "mixed";
        var stateWord = st === "true" ? "all armed" : st === "false" ? "all masked" : "mixed";
        b.className = "zonebtn " + (armed === total ? "armed" : armed === 0 ? "masked" : "mixed");
        var side = c2 === 0 ? "left" : "right";
        b.textContent = "R" + (r0 + 1) + "\u2013" + (r0 + ZONE_H) + " " + side;
        b.setAttribute("aria-label", "Rows " + (r0 + 1) + "\u2013" + (r0 + ZONE_H) + ", " +
          side + " half: " + armed + " of " + total + " cells armed, " + stateWord +
          " \u2014 apply " + action);
      }
    }
    function updateUndoRedo() {
      var u = document.getElementById("undo"), r = document.getElementById("redo");
      if (u) u.disabled = !state.undo.length;
      if (r) r.disabled = !state.redo.length;
    }
    function updateDirty() {
      var dirty = isDirty();
      if (dirtyBar) dirtyBar.hidden = !dirty;
      if (dirtyText) {
        dirtyText.textContent = dirty
          ? "Unsaved changes \u2014 the camera still has " + summaryText(state.camera.cells) +
            ", sensitivity " + state.camera.sensitivity +
            ", motion detection " + (state.camera.enabled ? "on" : "off") + "."
          : "";
      }
      var d = document.getElementById("motion-discard");
      if (d) d.hidden = !dirty;
    }

    /* ── zoom / pan ── */
    function applyZoomWidth() {
      canvas.style.width = state.fit ? "" : (state.zoom * 100) + "%";
      var readout = document.getElementById("zoom-readout");
      if (readout) readout.textContent = state.fit ? "Fit" : (Math.round(state.zoom * 100) + "%");
      var hint = document.getElementById("zone-hint");
      if (hint) hint.hidden = state.zoom > 1.05 || window.innerWidth >= 720;
    }
    function setZoom(z, clientX, clientY) {
      z = Math.max(1, Math.min(6, z));
      var oldRect = canvas.getBoundingClientRect();
      var px = clientX !== undefined ? clientX - oldRect.left : null;
      var py = clientY !== undefined ? clientY - oldRect.top : null;
      state.zoom = z;
      state.fit = z <= 1.001;
      applyZoomWidth();
      if (px !== null) {
        var newRect = canvas.getBoundingClientRect();
        viewport.scrollLeft += px * (newRect.width / oldRect.width) - px;
        viewport.scrollTop += py * (newRect.height / oldRect.height) - py;
      }
    }

    /* ── pointer input (mouse, touch, pen) ── */
    function cellFromClient(clientX, clientY) {
      var rect = grid.getBoundingClientRect();
      var c = Math.floor((clientX - rect.left) / rect.width * COLS);
      var r = Math.floor((clientY - rect.top) / rect.height * ROWS);
      if (!isFinite(r)) r = 0;
      if (!isFinite(c)) c = 0;
      return { r: Math.min(ROWS - 1, Math.max(0, r)), c: Math.min(COLS - 1, Math.max(0, c)) };
    }
    function pointerCount() { return Object.keys(state.pointers).length; }
    function pinchPoints() {
      var ids = Object.keys(state.pointers);
      return [state.pointers[ids[0]], state.pointers[ids[1]]];
    }
    function startPinch() {
      var p = pinchPoints();
      var d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1;
      state.pinch = { d0: d, zoom0: state.zoom };
    }
    function updatePinch() {
      var p = pinchPoints();
      var d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1;
      setZoom(state.pinch.zoom0 * (d / state.pinch.d0),
        (p[0].x + p[1].x) / 2, (p[0].y + p[1].y) / 2);
    }

    grid.addEventListener("pointerdown", function (e) {
      if (!state.ready || state.applying) return;
      // Reject a non-left mouse button before any state changes: otherwise a
      // right-click during a left-drag cancels the stroke and fakes a pinch.
      if (e.pointerType === "mouse" && e.button !== 0) return;
      state.pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      if (state.mode === "pan") return;
      if (pointerCount() === 2) { state.stroke = null; startPinch(); return; }
      if (pointerCount() > 2) return;
      e.preventDefault();
      try { grid.setPointerCapture(e.pointerId); } catch (err) { /* noop */ }
      var cell = cellFromClient(e.clientX, e.clientY);
      state.stroke = { before: beginStroke(), r: cell.r, c: cell.c };
      paintBrush(cell.r, cell.c);
      moveCursor(cell.r, cell.c);
    });

    grid.addEventListener("pointermove", function (e) {
      if (state.pointers[e.pointerId]) {
        state.pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      }
      if (state.pinch && pointerCount() >= 2) { updatePinch(); return; }
      if (state.mode === "pan" || !state.stroke) return;
      e.preventDefault();
      var cell = cellFromClient(e.clientX, e.clientY);
      var from = state.stroke;
      lineTo(from.r, from.c, cell.r, cell.c, function (r, c) { paintBrush(r, c); });
      from.r = cell.r;
      from.c = cell.c;
      moveCursor(cell.r, cell.c);
    });

    function endPointer(e) {
      delete state.pointers[e.pointerId];
      if (state.pinch && pointerCount() < 2) { state.pinch = null; state.stroke = null; }
      if (state.stroke) {
        var before = state.stroke.before;
        state.stroke = null;
        commitStroke(before);
      }
    }
    grid.addEventListener("pointerup", endPointer);
    grid.addEventListener("pointercancel", endPointer);
    grid.addEventListener("lostpointercapture", endPointer);

    viewport.addEventListener("wheel", function (e) {
      if (!state.ready) return;
      e.preventDefault();
      setZoom(state.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15), e.clientX, e.clientY);
    }, { passive: false });

    /* ── keyboard on the grid: roving cursor ── */
    grid.addEventListener("keydown", function (e) {
      if (!state.ready) return;
      var r = state.cursor.r, c = state.cursor.c, handled = true;
      switch (e.key) {
        case "ArrowUp": r = Math.max(0, r - 1); break;
        case "ArrowDown": r = Math.min(ROWS - 1, r + 1); break;
        case "ArrowLeft": c = Math.max(0, c - 1); break;
        case "ArrowRight": c = Math.min(COLS - 1, c + 1); break;
        case "Home": c = 0; break;
        case "End": c = COLS - 1; break;
        case "PageUp": r = Math.max(0, r - 3); break;
        case "PageDown": r = Math.min(ROWS - 1, r + 3); break;
        case " ": case "Enter": case "Spacebar":
          var before = beginStroke();
          paintBrush(r, c);
          commitStroke(before);
          e.preventDefault();
          return;
        default: handled = false;
      }
      if (handled) { moveCursor(r, c); e.preventDefault(); }
    });

    document.addEventListener("keydown", function (e) {
      if (!state.ready) return;
      var tag = (e.target && e.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.ctrlKey || e.metaKey) {
        if (e.key === "z" || e.key === "Z") {
          if (e.shiftKey) redo(); else undo();
          e.preventDefault();
        }
        return;
      }
      if (e.key === "a") setTool("arm");
      else if (e.key === "m") setTool("mask");
      else if (e.key === "i") doInvert();
      else if (e.key === "+" || e.key === "=") zoomIn();
      else if (e.key === "-" || e.key === "_") zoomOut();
      else if (e.key === "0") zoomFit();
    });

    /* ── tools & bulk ── */
    function setTool(tool) {
      state.tool = tool;
      Array.prototype.forEach.call(document.querySelectorAll("#tool-group .segbtn"), function (b) {
        b.setAttribute("aria-pressed", b.dataset.tool === tool ? "true" : "false");
      });
      if (state.draft) renderCoarse();  // quick-zone names state the tool's action
    }
    function fillAll(armed) {
      if (!state.ready) return;
      var before = beginStroke();
      for (var r = 0; r < ROWS; r++) for (var c = 0; c < COLS; c++) state.draft.cells[r][c] = armed;
      renderCellsAll();
      commitStroke(before);
    }
    function doInvert() {
      if (!state.ready) return;
      var before = beginStroke();
      for (var r = 0; r < ROWS; r++) for (var c = 0; c < COLS; c++) {
        state.draft.cells[r][c] = !state.draft.cells[r][c];
      }
      renderCellsAll();
      commitStroke(before);
    }
    function undo() {
      if (!state.undo.length) return;
      state.redo.push(cloneCells(state.draft.cells));
      state.draft.cells = state.undo.pop();
      renderCellsAll();
      afterEdit();
    }
    function redo() {
      if (!state.redo.length) return;
      state.undo.push(cloneCells(state.draft.cells));
      state.draft.cells = state.redo.pop();
      renderCellsAll();
      afterEdit();
    }
    function zoomIn() { setZoom(state.fit ? 1.5 : state.zoom * 1.5); }
    function zoomOut() { setZoom(state.zoom / 1.5); }
    function zoomFit() { setZoom(1); viewport.scrollTop = 0; viewport.scrollLeft = 0; }

    /* ── notes / errors ── */
    function showError(html, retry) {
      if (!errorBox) return;
      errorBox.hidden = !html;
      if (!html) { errorBox.innerHTML = ""; return; }
      errorBox.innerHTML = "<p>" + html + "</p>" +
        (retry
          ? "<p><button type='button' class='copybtn' id='motion-retry'>" +
            "Retry reading the camera</button></p>"
          : "");
      var retryBtn = document.getElementById("motion-retry");
      if (retryBtn) retryBtn.addEventListener("click", function () { showError(""); load(); });
    }
    /* `html` is caller-built markup (paragraphs and lists kept as siblings). */
    function showEndnote(warn, html, highlightOn) {
      if (!endnote) return;
      endnote.className = "endnote" + (warn ? " warn" : "");
      endnote.innerHTML = html +
        (highlightOn ? "<p><a href='/'>See the armed zones on Live</a></p>" : "");
      endnote.hidden = false;
    }
    function hideEndnote() {
      if (endnote) { endnote.hidden = true; endnote.innerHTML = ""; }
    }
    function savedText(actual) {
      return "Saved. The camera now has " + countArmed(actual.cells) + " of " +
        (ROWS * COLS) + " cells armed, sensitivity " + actual.sensitivity +
        ", motion detection " + (actual.enabled ? "on" : "off") + ".";
    }

    /* ── apply (readback is truth) ── */
    function setBusy(busy) {
      ["motion-apply", "motion-apply-top"].forEach(function (id) {
        var b = document.getElementById(id);
        if (b) { b.disabled = busy; b.textContent = busy ? "Applying\u2026" : "Apply to camera"; }
      });
    }
    function apply() {
      if (!state.ready || state.applying) return;
      state.applying = true;
      setBusy(true);
      hideEndnote();
      showError("");
      setStatus("spin", "Sending to the camera\u2026");
      fetch("/api/motion", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: state.draft.enabled,
          enable_highlight: state.draft.enable_highlight,
          sensitivity: state.draft.sensitivity,
          cells: state.draft.cells
        })
      }).then(function (r) {
        return r.json().then(function (body) { return { status: r.status, body: body }; });
      }).then(function (res) {
        handleApply(res.status, res.body);
      }).catch(function (err) {
        showError("The viewer could not be reached: " + err);
        setStatus("err", "The viewer could not be reached.");
      }).then(function () {
        state.applying = false;
        setBusy(false);
      });
    }
    function handleApply(status, body) {
      if (status === 403 || (body && body.state === "forbidden")) {
        showError("The viewer refused this request (cross-origin or wrong content type).");
        setStatus("err", "Request refused.");
        return;
      }
      if (!body || typeof body !== "object") {
        showError("Unexpected reply from the viewer.");
        setStatus("err", "Unexpected reply.");
        return;
      }
      if (body.state === "ok") {
        var actual = body.actual;
        state.camera = fromPayload(actual);
        state.draft = cloneState(state.camera);
        state.undo = [];
        state.redo = [];
        renderAll();
        if (body.match) {
          showEndnote(false, "<p>" + escapeHtml(savedText(actual)) + "</p>",
            actual.enable_highlight);
          setStatus("ok", "Saved \u2014 reported from the camera's readback.");
        } else {
          var items = (body.differences || []).map(function (d) {
            return "<li>" + escapeHtml(d) + "</li>";
          }).join("");
          // The list must be a sibling of the paragraphs, not nested inside one.
          showEndnote(true,
            "<p>Saved with drift. The camera adjusted the values:</p><ul>" + items + "</ul>" +
            "<p>" + escapeHtml(savedText(actual)) + "</p>",
            actual.enable_highlight);
          setStatus("warn", "Saved with drift \u2014 the camera changed the values.");
        }
        return;
      }
      if (body.state === "rejected") {
        showError("The camera rejected this change (403 Invalid Operation). " +
          escapeHtml(body.state_detail || "") + " Nothing was saved.");
        setStatus("warn", "The camera rejected the change.");
        return;
      }
      if (body.write_attempted) {
        // The write went out but the camera never confirmed it: claiming either
        // "saved" or "nothing saved" would be an unobserved claim.
        var seen = body.actual
          ? " The camera currently reads " + countArmed(body.actual.cells) + " of " +
            (ROWS * COLS) + " cells armed, sensitivity " + body.actual.sensitivity +
            ", motion detection " + (body.actual.enabled ? "on" : "off") + "."
          : " The camera could not be read back, so its current state is unknown.";
        showError(
          "The viewer could not confirm the write \u2014 it may or may not have been saved." +
          seen + " Re-read the camera before retrying." +
          (body.state_detail ? " (" + escapeHtml(body.state_detail) + ")" : ""),
          true);
        setStatus("warn", "The write may not have landed \u2014 re-read the camera.");
        return;
      }
      var messages = {
        unreachable: "The camera is unreachable. Nothing was saved.",
        service_down: "The camera did not answer. Nothing was saved.",
        bad_response: "The camera gave an unexpected reply (possible firmware drift).",
        bad_request: "The viewer rejected the request."
      };
      showError((messages[body.state] || "Could not save.") +
        (body.state_detail ? " (" + escapeHtml(body.state_detail) + ")" : ""),
        body.state === "bad_response");
      setStatus("err", messages[body.state] || "Could not save.");
    }
    function discard() {
      if (!state.ready) return;
      state.draft = cloneState(state.camera);
      state.undo = [];
      state.redo = [];
      hideEndnote();
      showError("");
      renderAll();
      setStatus("ok", "Changes discarded \u2014 the editor shows the camera again.");
    }

    /* ── load ── */
    function load() {
      setStatus("spin", "Reading the camera\u2026");
      hideEndnote();
      fetch("/api/motion", { cache: "no-store" }).then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data || data.state !== "ok") {
            var known = {
              unreachable: "The camera is unreachable.",
              service_down: "The camera did not answer.",
              bad_response: "The camera returned an unexpected zone format."
            };
            var msg = known[data && data.state] || "Could not read the camera's zones.";
            var detail = data && data.state_detail
              ? " (" + escapeHtml(data.state_detail) + ")"
              : "";
            var raw = data && data.state === "bad_response"
              ? " <a href=\"/isapi/System/Video/inputs/channels/1/motionDetection\">" +
                "View the camera's raw reply</a>."
              : "";
            showError(msg + detail + raw, true);
            renderUnknown();
            setStatus("err", msg);
            return;
          }
          state.camera = fromPayload(data);
          state.draft = cloneState(state.camera);
          if (data.resolution && data.resolution.length === 2) {
            canvas.style.aspectRatio = data.resolution[0] + " / " + data.resolution[1];
          }
          if (Array.isArray(data.valid_sensitivities) && data.valid_sensitivities.length) {
            SENS = data.valid_sensitivities;
          }
          renderAll();
          setReady(true);
          setStatus("ok", "Read the camera's current zones.");
        })
        .catch(function (err) {
          showError("The viewer could not be reached: " + err, true);
          renderUnknown();
          setStatus("err", "The viewer could not be reached.");
        });
    }

    /* ── wire controls ── */
    Array.prototype.forEach.call(document.querySelectorAll("#tool-group .segbtn"), function (b) {
      b.addEventListener("click", function () { setTool(b.dataset.tool); });
    });
    Array.prototype.forEach.call(document.querySelectorAll("#brush-group .segbtn"), function (b) {
      b.addEventListener("click", function () {
        state.brush = Number(b.dataset.brush);
        Array.prototype.forEach.call(document.querySelectorAll("#brush-group .segbtn"), function (x) {
          x.setAttribute("aria-pressed", x === b ? "true" : "false");
        });
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll("#mode-group .segbtn"), function (b) {
      b.addEventListener("click", function () {
        state.mode = b.dataset.mode;
        grid.classList.toggle("panning", state.mode === "pan");
        Array.prototype.forEach.call(document.querySelectorAll("#mode-group .segbtn"), function (x) {
          x.setAttribute("aria-pressed", x === b ? "true" : "false");
        });
      });
    });
    document.getElementById("motion-enabled").addEventListener("change", function (e) {
      if (!state.ready || !state.draft) return;
      state.draft.enabled = e.target.checked;
      afterEdit();
    });
    document.getElementById("motion-highlight").addEventListener("change", function (e) {
      if (!state.ready || !state.draft) return;
      state.draft.enable_highlight = e.target.checked;
      afterEdit();
    });
    document.getElementById("arm-all").addEventListener("click", function () { fillAll(true); });
    document.getElementById("mask-all").addEventListener("click", function () { fillAll(false); });
    document.getElementById("invert-all").addEventListener("click", doInvert);
    document.getElementById("undo").addEventListener("click", undo);
    document.getElementById("redo").addEventListener("click", redo);
    document.getElementById("zoom-in").addEventListener("click", zoomIn);
    document.getElementById("zoom-out").addEventListener("click", zoomOut);
    document.getElementById("zoom-fit").addEventListener("click", zoomFit);
    document.getElementById("toggle-photo").addEventListener("click", function (e) {
      var hidden = card.classList.toggle("nophoto");
      e.target.textContent = hidden ? "Show photo" : "Hide photo";
    });
    document.getElementById("zone-coarse").addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest(".zonebtn") : null;
      if (!btn || !state.ready) return;
      var r0 = Number(btn.dataset.zoneRow) * ZONE_H, c0 = Number(btn.dataset.zoneCol) * ZONE_W;
      var arm = state.tool === "arm";
      var before = beginStroke();
      for (var rr = r0; rr < r0 + ZONE_H; rr++)
        for (var cc = c0; cc < c0 + ZONE_W; cc++) setCell(rr, cc, arm);
      renderCellsAll();
      commitStroke(before);
    });
    document.getElementById("motion-apply").addEventListener("click", apply);
    document.getElementById("motion-apply-top").addEventListener("click", apply);
    document.getElementById("motion-discard").addEventListener("click", discard);
    document.getElementById("motion-discard-top").addEventListener("click", discard);

    window.addEventListener("beforeunload", function (e) {
      if (state.ready && isDirty()) { e.preventDefault(); e.returnValue = ""; }
    });

    // Below 720px the secondary controls (sensitivity, zoom, bulk, shortcuts)
    // start collapsed so Apply stays near the top; desktop keeps them expanded.
    var mobileQuery = window.matchMedia ? window.matchMedia("(max-width: 719px)") : null;
    function syncSecondary() {
      var sec = document.getElementById("zone-secondary");
      if (!sec || !mobileQuery) return;
      if (mobileQuery.matches) sec.removeAttribute("open");
      else sec.setAttribute("open", "");
    }
    if (mobileQuery) {
      if (mobileQuery.addEventListener) mobileQuery.addEventListener("change", syncSecondary);
      else if (mobileQuery.addListener) mobileQuery.addListener(syncSecondary);
    }
    syncSecondary();

    buildGrid();
    renderUnknown();
    load();
  }

  /* Shared formatting/jump helpers (used by the picker and the player). */
  function humanDuration(seconds) {
    var s = Math.max(0, Math.round(seconds));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    if (h) return h + " h " + String(m).padStart(2, "0") + " min";
    if (m) return m + " min " + String(r).padStart(2, "0") + " s";
    return r + " s";
  }

  function humanSize(bytes) {
    var units = ["B", "KB", "MB", "GB"];
    var value = Math.max(0, bytes), i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return (i === 0 ? value : value.toFixed(1)) + " " + units[i];
  }

  /* Camera time format: 20261013T080617Z (UTC, compact). */
  function camTime(epoch) {
    return new Date(epoch * 1000).toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  }

  function playHref(fromEpoch, toEpoch, size) {
    var dur = Math.max(1, toEpoch - fromEpoch);
    return "/play?from=" + camTime(fromEpoch) + "&to=" + camTime(toEpoch) +
      "&dur=" + dur + "&size=" + (size || 0);
  }

  /* ── boot ─────────────────────────────────────────────────────── */

  document.addEventListener("DOMContentLoaded", function () {
    var live = document.getElementById("live-video");
    var clip = document.getElementById("clip-video");
    if (live) initLive(live);
    if (clip) initPlayback(clip);
    initPicker();
    if (document.getElementById("zone-grid")) initMotion();

    document.querySelectorAll("form.search").forEach(function (form) {
      form.addEventListener("submit", function () {
        var btn = form.querySelector(".searchbtn");
        if (btn) { btn.disabled = true; btn.textContent = "Searching the camera…"; }
      });
    });

    document.querySelectorAll(".copybtn").forEach(function (btn) {
      // Only real copy buttons (those naming a copy target) own the label.
      // The motion editor reuses .copybtn for styling, and relabelling those
      // "Hide photo"/"Undo"/"Arm all" controls to "Copied" was a lie.
      if (!btn.dataset.target) return;
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
