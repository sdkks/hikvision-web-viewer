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
    const rangeRows = document.getElementById("range-rows");
    const playBar = document.getElementById("play-bar");
    const playFrom = document.getElementById("play-from");

    const cache = new Map();
    const state = { date: null, hour: null, minute: null, coverage: null };
    const pad = (n) => String(n).padStart(2, "0");
    const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const camTime = (epoch) =>
      new Date(epoch * 1000).toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
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

    function rangesInHour(hour) {
      const dayStart = new Date(`${state.coverage.date}T00:00:00`).getTime() / 1000;
      const from = dayStart + hour * 3600;
      const to = from + 3600;
      return state.coverage.ranges.filter((r) => r.start_epoch < to && r.end_epoch > from);
    }

    function renderHour() {
      const hour = state.hour;
      const minutes = state.coverage.minutes[hour];
      const ranges = rangesInHour(hour);
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
        rangeRows.textContent = "";
        return;
      }
      const initial = state.minute !== null && minutes[state.minute] > 0 ? state.minute : covered[0];
      scrubber.value = String(initial);
      updateMinute(initial, { push: false });

      rangeRows.textContent = "";
      for (const range of ranges) {
        const row = document.createElement("div");
        row.className = "range-row";
        row.innerHTML =
          `<span class="when">${hhmm(range.start_epoch)} → ${hhmm(range.end_epoch)}</span>` +
          `<span class="meta">${humanDuration(range.dur_s)} · ${humanSize(range.size)}` +
          (range.clips > 1 ? ` · ${range.clips} clips` : "") + `</span>`;
        const acts = document.createElement("span");
        acts.className = "acts";
        const play = document.createElement("a");
        play.className = "playbtn";
        play.textContent = "▶ Play";
        play.href = playHref(range.start_epoch, range.end_epoch, range.size);
        const dl = document.createElement("a");
        dl.className = "dlbtn";
        dl.textContent = "⬇ Download";
        dl.href = `/download?from=${camTime(range.start_epoch)}&to=${camTime(range.end_epoch)}&size=${range.size}`;
        acts.append(play, dl);
        row.append(acts);
        rangeRows.append(row);
      }
    }

    function humanDuration(seconds) {
      const s = Math.max(0, Math.round(seconds));
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
      if (h) return `${h} h ${pad(m)} min`;
      if (m) return `${m} min ${pad(r)} s`;
      return `${r} s`;
    }

    function humanSize(bytes) {
      const units = ["B", "KB", "MB", "GB"];
      let value = Math.max(0, bytes), i = 0;
      while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
      return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`;
    }

    function playHref(fromEpoch, toEpoch, size) {
      const dur = Math.max(1, toEpoch - fromEpoch);
      return `/play?from=${camTime(fromEpoch)}&to=${camTime(toEpoch)}&dur=${dur}&size=${size}`;
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

  /* ── boot ─────────────────────────────────────────────────────── */

  document.addEventListener("DOMContentLoaded", function () {
    var live = document.getElementById("live-video");
    var clip = document.getElementById("clip-video");
    if (live) initLive(live);
    if (clip) initPlayback(clip);
    initPicker();

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
