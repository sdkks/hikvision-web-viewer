/* hikvision-web-viewer client: MSE/ManagedMediaSource fMP4 streaming, ~no framework. */
(function () {
  "use strict";

  /* ── MSE plumbing ─────────────────────────────────────────────── */

  function pickMediaSourceCtor() {
    if (window.ManagedMediaSource) return window.ManagedMediaSource;
    if (window.MediaSource) return window.MediaSource;
    return null;
  }

  async function canDecodeInPractice(codecString) {
    // MediaSource.isTypeSupported over-reports (e.g. HEVC on Chrome without a decoder).
    // WebCodecs' isConfigSupported reflects what can actually decode here.
    if (window.VideoDecoder && VideoDecoder.isConfigSupported) {
      try {
        const r = await VideoDecoder.isConfigSupported({ codec: codecString });
        if (r && r.supported === false) return false;
      } catch (e) { /* keep optimistic */ }
    }
    return true;
  }

  async function pickCodec(candidates) {
    const msCtor = pickMediaSourceCtor();
    if (!msCtor || !msCtor.isTypeSupported) return null;
    for (const candidate of candidates) {
      if (msCtor.isTypeSupported(candidate) && (await canDecodeInPractice(candidate))) {
        return candidate;
      }
    }
    return null;
  }

  var CODEC_SETS = {
    // Main channel is H.265 on this camera: HEVC-first (Safari), AVC fallback (transcoded cams).
    hevc: ['video/mp4; codecs="hvc1.1.6.L93.B0"', 'video/mp4; codecs="hvc1.1.6.L120.90"',
           'video/mp4; codecs="avc1.640029"', 'video/mp4; codecs="avc1.42E01E"'],
    // Sub channel / H.264 sources: AVC-first.
    avc: ['video/mp4; codecs="avc1.640029"', 'video/mp4; codecs="avc1.42E01E"',
          'video/mp4; codecs="hvc1.1.6.L93.B0"'],
  };

  /* Split an fMP4 byte stream into MSE-ready segments:
     init segment = boxes before the first moof; each media segment = moof + trailing boxes. */
  function BoxSplitter(onInit, onSegment) {
    this.buf = null;
    this.onInit = onInit;
    this.onSegment = onSegment;
    this.inMedia = false;
    this.pending = [];
  }

  BoxSplitter.prototype.push = function (u8) {
    this.buf = this.buf ? concat(this.buf, u8) : u8;
    var off = 0;
    for (;;) {
      if (this.buf.length - off < 8) break;
      var view = new DataView(this.buf.buffer, this.buf.byteOffset + off);
      var size = view.getUint32(0);
      if (size === 1) {
        if (this.buf.length - off < 16) break;
        size = Number(view.getBigUint64(8));
      } else if (size === 0) {
        size = this.buf.length - off; // "to end of file"
      }
      if (size < 8) { this.fail(); return; }
      if (this.buf.length - off < size) break;
      var type = String.fromCharCode(
        this.buf[off + 4], this.buf[off + 5], this.buf[off + 6], this.buf[off + 7]);
      var box = this.buf.subarray(off, off + size);
      if (type === "moof") {
        this.flushPending();
        this.inMedia = true;
        this.pending.push(box);
      } else if (this.inMedia) {
        this.pending.push(box);
      } else {
        this.onInit(box);
      }
      off += size;
    }
    this.buf = this.buf.length > off ? this.buf.subarray(off) : null;
  };

  BoxSplitter.prototype.flushPending = function () {
    if (this.pending.length) {
      var seg = concat.apply(null, this.pending);
      this.pending = [];
      this.onSegment(seg);
    }
  };

  BoxSplitter.prototype.finish = function () { this.flushPending(); };

  BoxSplitter.prototype.fail = function () {
    this.buf = null;
    this.onInit = this.onSegment = function () {};
  };

  function concat() {
    var total = 0, i;
    for (i = 0; i < arguments.length; i++) total += arguments[i].length;
    var out = new Uint8Array(total), pos = 0;
    for (i = 0; i < arguments.length; i++) { out.set(arguments[i], pos); pos += arguments[i].length; }
    return out;
  }

  /* ── stream attach ────────────────────────────────────────────── */

  function attach(video, url, opts) {
    var state = {
      attempts: 0,
      aborted: false,
      controller: new AbortController(),
      sb: null,
      ms: null,
      queue: [],
      splitter: null,
    };

    function setStatus(kind, text) {
      if (opts.onStatus) opts.onStatus(kind, text, state);
    }

    function feed() {
      if (state.sb && !state.sb.updating && state.queue.length) {
        try { state.sb.appendBuffer(state.queue.shift()); }
        catch (e) { setStatus("err", "Playback buffer error — try Replay."); state.aborted = true; }
      }
    }

    function trimLiveBuffer() {
      if (!opts.live || !state.sb || state.sb.updating) return;
      try {
        var end = state.sb.buffered.length ? state.sb.buffered.end(0) : 0;
        if (end > 60) state.sb.remove(0, end - 30);
      } catch (e) { /* buffered can be empty between appends */ }
    }

    async function openSource() {
      var MS = pickMediaSourceCtor();
      var codec = await pickCodec(opts.codecs || CODEC_SETS.avc);
      if (!MS || !codec) {
        setStatus("nosupport",
          "This browser can't decode the camera stream. Use the still-image links below.");
        return;
      }
      var ms = new MS();
      state.ms = ms;
      video.src = URL.createObjectURL(ms);
      console.log("[viewer] ms created, mms=" + (MS === window.ManagedMediaSource) + ", codec=" + codec);
      // ManagedMediaSource only opens its source once the element starts playing;
      // kick it immediately (muted autoplay is allowed) to avoid a sourceopen deadlock.
      video.play().catch(function () { /* retried after data arrives */ });
      ms.addEventListener("sourceopen", function () {
        if (state.aborted) return;
        console.log("[viewer] sourceopen");
        var sb;
        try { sb = ms.addSourceBuffer(codec); }
        catch (e) { console.log("[viewer] addSourceBuffer threw: " + e); setStatus("err", "Couldn't initialise player buffer."); return; }
        state.sb = sb;
        sb.addEventListener("updateend", function () { feed(); trimLiveBuffer(); });
        sb.addEventListener("error", function () { console.log("[viewer] SOURCEBUFFER ERROR"); setStatus("err", "Player buffer error — the camera stream may have changed codecs."); });
        ms.addEventListener("sourceclose", function () { console.log("[viewer] sourceclose"); });
        sb.mode = "segments";
        console.log("[viewer] sb ready, connecting");
        connect();
      });
    }

    async function connect() {
      console.log("[viewer] connect() fetching " + url);
      setStatus("connect", opts.live ? "Connecting…" : "Connecting to clip… starts at its beginning.");
      var firstByteSeen = false;
      var initBoxes = [];
      var initFlushed = false;
      try {
        var res = await fetch(url, { signal: state.controller.signal, cache: "no-store" });
        if (!res.ok || !res.body) {
          setStatus("err", "Couldn't open the stream (HTTP " + res.status + ").");
          return;
        }
        state.splitter = new BoxSplitter(
          function (initBox) {
            console.log("[viewer] init box " + initBox.length + "B");
            initBoxes.push(initBox);
          },
          function (seg) {
            if (!initFlushed) {
              const packed = concat.apply(null, initBoxes);
              console.log("[viewer] flushing init " + packed.length + "B + seg " + seg.length + "B");
              state.queue.push(packed);
              initBoxes = [];
              initFlushed = true;
            } else {
              console.log("[viewer] seg " + seg.length + "B");
            }
            state.queue.push(seg);
            feed();
          }
        );
        var reader = res.body.getReader();
        for (;;) {
          var chunk = await reader.read();
          if (chunk.done) break;
          if (!firstByteSeen) { firstByteSeen = true; setStatus("ok", opts.live ? "Live · ≈2 s behind" : "Playing"); }
          state.splitter.push(chunk.value);
          try { await video.play(); } catch (e) { /* user gesture needed; controls remain */ }
        }
        if (state.splitter) state.splitter.finish();
        onEnded();
      } catch (err) {
        if (state.aborted || (err && err.name === "AbortError")) return;
        setStatus("err", "Connection lost." + (opts.live ? "" : " The camera's streams usually recover — try Replay."));
        if (opts.live) scheduleReconnect();
      }
    }

    function scheduleReconnect() {
      state.attempts += 1;
      var delays = [2000, 4000, 8000, 15000, 30000];
      var delay = delays[Math.min(state.attempts - 1, delays.length - 1)];
      setStatus("warn", "Connection lost — reconnecting (attempt " + state.attempts + ")…");
      setTimeout(function () { if (!state.aborted) connect(); }, delay);
    }

    function onEnded() {
      if (opts.live) { scheduleReconnect(); return; }
      var expected = Number(video.dataset.expected || 0);
      var elapsed = Number(video.dataset.elapsed || 0);
      if (expected && elapsed < expected * 0.9) {
        setStatus("warn", "");
        showEnd(true,
          "Stream ended early — another playback session may have started (the camera allows one).");
      } else {
        showEnd(false, "Clip finished.");
      }
    }

    function showEnd(warn, text) {
      var note = document.getElementById("endnote");
      if (!note) return;
      note.className = "endnote" + (warn ? " warn" : "");
      note.innerHTML = "<p>" + text + "</p><div class='row'>" +
        "<button type='button' class='retry' onclick='window.location.reload()'>Replay</button>" +
        "<a class='retry' style='display:inline-flex;align-items:center;text-decoration:none' href='/recordings'>← Results</a></div>";
      note.hidden = false;
    }

    state.abort = function () {
      state.aborted = true;
      state.controller.abort();
      try { if (state.ms && state.ms.endOfSession) state.ms.endOfSession(); } catch (e) { /* noop */ }
    };

    state.debug = function () {
      var ranges = null;
      try {
        ranges = state.sb && state.sb.buffered.length
          ? [state.sb.buffered.start(0), state.sb.buffered.end(0)] : null;
      } catch (e) { /* noop */ }
      return { sbAttached: !!state.sb, queueLen: state.queue.length,
               msState: state.ms ? state.ms.readyState : null, buffered: ranges,
               attempts: state.attempts, aborted: state.aborted,
               videoReady: video.readyState, videoPaused: video.paused,
               videoErr: video.error ? String(video.error.code) : null };
    };
    window.__streamState = state;

    openSource();
    return state;
  }

  /* ── live page ────────────────────────────────────────────────── */

  var liveState = null;

  function initLive(video) {
    var statusLine = document.getElementById("status-line");
    var retryBtn = document.getElementById("retry");
    var current = video.dataset.channel || "101";

    function render(kind, text) {
      if (!statusLine) return;
      var dot = statusLine.querySelector(".dot");
      var label = statusLine.querySelector(".label");
      var map = {
        ok: ["dot ok", "Live · ≈2 s behind"],
        connect: ["dot spin", text || "Connecting…"],
        warn: ["dot warn", text],
        err: ["dot err", text],
        nosupport: ["dot warn", text],
      };
      var m = map[kind] || ["dot", text];
      if (dot) dot.className = "dot " + m[0].replace("dot ", "");
      if (label) label.textContent = m[1];
      if (retryBtn) retryBtn.hidden = !(kind === "err" || kind === "warn");
    }

    function start(channel) {
      if (liveState) { liveState.abort(); liveState = null; }
      video.dataset.channel = channel;
      video.dataset.expected = "";
      current = channel;
      var codecSet = video.dataset.codecs || (channel === "101" ? "hevc" : "avc");
      liveState = attach(video, "/stream/" + channel, {
        live: true,
        codecs: CODEC_SETS[codecSet] || CODEC_SETS.avc,
        onStatus: function (kind, text) { render(kind, text); },
      });
    }

    document.querySelectorAll(".chan").forEach(function (btn) {
      btn.addEventListener("click", function () {
        document.querySelectorAll(".chan").forEach(function (b) {
          b.setAttribute("aria-pressed", b === btn ? "true" : "false");
        });
        render("connect", "Switching…");
        start(btn.dataset.channel);
      });
    });

    if (retryBtn) retryBtn.addEventListener("click", function () {
      render("connect", "Connecting…");
      start(current);
    });

    start(current);
  }

  /* ── playback page ────────────────────────────────────────────── */

  function initPlayback(video) {
    var statusLine = document.getElementById("status-line");
    var counter = document.getElementById("counter");
    var progress = document.getElementById("progress");
    var expected = Number(video.dataset.expected || 0);
    var t0 = null;
    var ticker = null;

    function fmt(s) {
      s = Math.max(0, Math.floor(s));
      var m = Math.floor(s / 60), r = s % 60;
      var h = Math.floor(m / 60); m = m % 60;
      return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(r).padStart(2, "0");
    }

    function render(kind, text) {
      if (!statusLine) return;
      var dot = statusLine.querySelector(".dot");
      var label = statusLine.querySelector(".label");
      var map = {
        ok: ["dot ok", "Playing"],
        connect: ["dot spin", text || "Connecting…"],
        warn: ["dot warn", text],
        err: ["dot err", text],
        nosupport: ["dot warn", text],
      };
      var m = map[kind] || ["dot", text];
      if (dot) dot.className = "dot " + m[0].replace("dot ", "");
      if (label) label.textContent = m[1];
    }

    attach(video, video.dataset.url, {
      live: false,
      codecs: CODEC_SETS[video.dataset.codecs] || CODEC_SETS.hevc,
      onStatus: function (kind, text) {
        render(kind, text);
        if (kind === "ok" && !ticker) {
          t0 = Date.now();
          ticker = setInterval(function () {
            var elapsed = (Date.now() - t0) / 1000;
            if (counter) counter.textContent = fmt(elapsed) + " / " + fmt(expected);
            if (progress && expected) progress.style.width = Math.min(100, (elapsed / expected) * 100) + "%";
            if (video.dataset.elapsed) video.dataset.elapsed = String(Math.floor(elapsed));
          }, 1000);
        }
      },
    });
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
