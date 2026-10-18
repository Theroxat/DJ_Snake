/* DJ Snake demo backend. A fake bot that runs in the browser: one guild, one
 * player, one web session. It mirrors djsnake/ (controls.py, web.py,
 * sessions.py render_panel, playback.py loading lines) closely enough for the
 * demo, with fictional data and the CC-BY clips in media/ (credits:
 * docs/README.md).
 *
 * The Discord wrapper (index.html + discord.js) creates one instance and the
 * clip page iframe (player.html + shim.js) reaches it through
 * window.parent.DJDemoBackend.shared. player.html opened on its own creates a
 * standalone instance that is always playing (the old demo). */
(() => {
  "use strict";

  // Real Creative Commons Attribution films (credits: docs/README.md). Each
  // clip is a 25 s excerpt of the YouTube video; titles are the YouTube titles.
  const CATALOG = [
    ["Caminandes 3: Llamigos", "SkVqJ1SGeL0"],
    ["Cosmos Laundromat – First Cycle", "Y-rmzh0PI3c"],
    ["Big Buck Bunny", "aqz-KE-bpKQ"],
    ["Tears of Steel", "R6MlUcmOul8"],
    ["Sintel", "eRsGyueVLvQ"],
  ];
  // Directors of the films, as the "artist" in the fake stats.
  const ARTISTS = ["Pablo Vazquez", "Mathieu Auvray", "Sacha Goedegebure", "Ian Hubert", "Colin Levy"];
  const DJS = ["Maya", "Theo", "Jun", "you"]; // The Discord demo's members
  const DURATION = 25, START = 1, END = 24;
  const BASE_URL = "https://dj.example.com"; // PUBLIC_BASE_URL (fictional)
  const PREFIX = "!";
  const PRESETS = ["arcade", "minimal", "nuit", "vinyle", "club", "terminal"];
  const SKINS = PRESETS.concat(["perso"]);
  const PANEL_SHORT = 100;

  // code -> [HTTP status, French (web page), English (Discord)], as ControlError.
  const ERRORS = {
    not_in_voice: [409, "Le bot n'est pas dans un salon vocal.", "I'm not in a voice channel."],
    nothing_playing: [409, "Rien n'est en cours de lecture.", "No music is currently playing."],
    track_not_found: [404, "Ce morceau n'est plus dans la file.", "That track is not in the queue."],
    bad_index: [400, "Position invalide.", "Invalid position."],
    bad_url: [400,
      "Seuls les liens YouTube (vidéo ou playlist) et Spotify (titre, album ou playlist) sont acceptés.",
      "Only YouTube (video or playlist) and Spotify (track, album or playlist) links are accepted."],
    spotify_short_link: [400,
      "Les liens courts spotify.link ne sont pas pris en charge : ouvre-le et copie le lien open.spotify.com.",
      "Short spotify.link links are not supported: open it and copy the open.spotify.com link."],
  };
  class ControlError extends Error {
    constructor(code) {
      super(code);
      this.code = code;
      [this.status, this.french, this.english] = ERRORS[code];
    }
  }

  const short = (t, n) => { t = String(t); n = n || PANEL_SHORT; return t.length <= n ? t : t.slice(0, n - 1) + "…"; };

  // Link parsing, as spotify.check_url() and media.parse_youtube_url().
  function parseLink(raw) {
    const url = String(raw || "").trim();
    let m = /^spotify:(playlist|album|track):([A-Za-z0-9]{22})$/.exec(url);
    if (m) return { kind: "spotify", type: m[1], id: m[2] };
    let u;
    try { u = new URL(url); } catch (e) { throw new ControlError("bad_url"); }
    if (!/^https?:$/.test(u.protocol)) throw new ControlError("bad_url");
    const host = u.hostname.toLowerCase();
    if (host === "spotify.link" || host.endsWith(".spotify.link")) throw new ControlError("spotify_short_link");
    if (host === "open.spotify.com" || host === "play.spotify.com") {
      m = /^(?:\/intl-[a-z]{2}(?:-[a-z]{2})?)?(?:\/embed)?\/(playlist|album|track)\/([A-Za-z0-9]{22})\/?$/i.exec(u.pathname);
      if (!m) throw new ControlError("bad_url");
      return { kind: "spotify", type: m[1].toLowerCase(), id: m[2] };
    }
    if (/^(www\.|m\.|music\.)?youtube\.com$/.test(host)) {
      const v = u.searchParams.get("v"), list = u.searchParams.get("list");
      if (v && /^[\w-]{11}$/.test(v)) return { kind: "youtube", video: v, list: list || null };
      if (list) return { kind: "youtube", video: null, list };
      throw new ControlError("bad_url");
    }
    if (host === "youtu.be") {
      const v = u.pathname.slice(1);
      if (/^[\w-]{11}$/.test(v)) return { kind: "youtube", video: v, list: null };
    }
    throw new ControlError("bad_url");
  }

  function create(opts) {
    opts = opts || {};
    const standalone = !!opts.standalone;
    const base = opts.baseURI || document.baseURI;
    const now = () => performance.now() / 1000;
    const media = (k) => new URL("media/clip" + (k + 1) + ".webm", base).href;
    const thumb = (k) => new URL("media/thumb" + (k + 1) + ".jpg", base).href;

    let seq = 0, rot = 0;
    const mkTrack = (k) => {
      k = ((k % CATALOG.length) + CATALOG.length) % CATALOG.length;
      seq++;
      return {
        id: "demo" + seq, title: CATALOG[k][0], duration: DURATION,
        thumbnail: thumb(k), url: "https://www.youtube.com/watch?v=" + CATALOG[k][1],
        video_url: media(k),
      };
    };
    const trackFor = (videoId) => {
      const k = CATALOG.findIndex((c) => c[1] === videoId);
      return k >= 0 ? k : rot++;
    };

    const S = {
      sid: null, inVoice: false, current: null, queue: [], history: [],
      startedAt: 0, pausedAt: null, pausedTotal: 0,
      crossfade: false, epoch: 0, jobs: 0, single: 0,
      loadLine: null, loadFinal: false, lastAction: null,
      theme: { skin: "arcade", base: "nuit", bg: "#101820", text: "#f2f2f2", accent: "#ffb703", name: "", logo_url: null },
    };

    // ---- events: {type: "change"} on any state change, "voice" on join/leave,
    // "panel-post" when the bot posts a fresh status panel, "web" for a web control.
    const listeners = new Set();
    const emit = (type, extra) => {
      const ev = Object.assign({ type }, extra || {});
      listeners.forEach((fn) => { try { fn(ev); } catch (e) { console.error(e); } });
    };
    const changed = () => emit("change");
    const on = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };

    const paused = () => S.pausedAt !== null;
    const elapsed = () => START + Math.max(0, (paused() ? S.pausedAt : now()) - S.startedAt - S.pausedTotal);
    function play(t) { S.current = t; S.startedAt = now(); S.pausedAt = null; S.pausedTotal = 0; }

    function setAction(text) {
      S.lastAction = text;
      if (S.loadFinal) { S.loadLine = null; S.loadFinal = false; }
      changed();
    }
    function progress(text, final) { S.loadLine = text; S.loadFinal = !!final; changed(); }

    function leaveVoice() {
      S.inVoice = false;
      S.current = null;
      S.pausedAt = null;
      emit("voice", { inVoice: false });
    }

    // ---- playback chain (playback.py _advance)
    function advance(action) {
      if (action === "previous" && S.history.length) {
        S.queue.unshift(S.current);
        play(S.history.pop());
        return;
      }
      if (S.current) S.history.push(S.current);
      S.history = S.history.slice(-5);
      if (standalone) while (S.queue.length < 4) S.queue.push(mkTrack(rot++));
      if (S.queue.length) play(S.queue.shift());
      else if (S.jobs > 0) S.current = null; // loading: wait for the next track
      else leaveVoice(); // queue ended: leave voice, the session stays
      changed();
    }
    function addToQueue(t) {
      S.queue.push(t);
      if (!S.current && S.inVoice) advance();
      changed();
    }
    setInterval(() => { if (S.current && !paused() && elapsed() >= END) advance(); }, 250);

    // ---- session and voice
    const sidChars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    function newSid() { let s = ""; for (let i = 0; i < 22; i++) s += sidChars[Math.floor(Math.random() * 64)]; return s; }
    /** Join voice and open (or reuse) the session; posts a fresh panel. */
    function join() {
      if (S.inVoice) return false;
      S.inVoice = true;
      if (!S.sid) S.sid = newSid();
      S.epoch++;
      emit("voice", { inVoice: true });
      emit("panel-post");
      changed();
      return true;
    }
    function openSession() {
      if (S.sid) return false;
      S.sid = newSid();
      emit("panel-post");
      changed();
      return true;
    }

    // ---- loading (fake downloads with realistic delays)
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const cancelled = (epoch) => epoch !== S.epoch || !S.inVoice;
    /** Load a list of catalog indexes one by one, like queue_playlist(). */
    async function loadList(ks, readLine) {
      const epoch = S.epoch, total = ks.length;
      let loaded = 0;
      S.jobs++;
      if (readLine) { progress(readLine); await wait(1100); }
      progress(`📥 Loading 0/${total}…`);
      try {
        for (const k of ks) {
          await wait(loaded === 0 ? 900 : 1400);
          if (cancelled(epoch)) break;
          loaded++;
          addToQueue(mkTrack(k));
          progress(`📥 Loading ${loaded}/${total}…`);
        }
      } finally { S.jobs = Math.max(0, S.jobs - 1); }
      if (cancelled(epoch)) {
        progress(`⏹️ Loading stopped (${loaded}/${total})`, true);
        return;
      }
      progress(`✅ ${loaded}/${total} tracks loaded`, true);
      if (!S.current && !S.queue.length && S.jobs === 0) leaveVoice();
    }
    async function loadOne(k, readLine) {
      const epoch = S.epoch;
      if (readLine) { progress(readLine); await wait(900); S.loadLine = null; }
      S.single++; changed();
      try { await wait(1300); } finally { S.single = Math.max(0, S.single - 1); }
      if (cancelled(epoch)) { changed(); return; }
      const t = mkTrack(k);
      S.queue.push(t);
      setAction(`➕ Added: ${short(t.title)}`);
      if (!S.current) advance();
    }
    /** Queue whatever a link points to (after parseLink). */
    function queueLink(link) {
      if (link.kind === "spotify") {
        if (link.type === "track") return loadOne(rot++, "🔎 Reading the Spotify link…");
        const n = link.type === "playlist" ? 5 : 3;
        const ks = [];
        for (let i = 0; i < n; i++) ks.push(link.type === "playlist" ? i : rot++);
        return loadList(ks, "🔎 Reading the Spotify link…");
      }
      if (link.video) return loadOne(trackFor(link.video), null);
      const ks = [rot++, rot++, rot++];
      return loadList(ks, "🔎 Analyzing the link…");
    }

    // ---- controls (controls.py), shared by Discord commands and the web page
    function requirePlaying() {
      if (!S.inVoice) throw new ControlError("not_in_voice");
      if (!S.current) throw new ControlError("nothing_playing");
    }
    const findQueued = (id) => {
      const i = S.queue.findIndex((t) => t.id === id);
      if (i < 0) throw new ControlError("track_not_found");
      return i;
    };
    const C = {
      next() { requirePlaying(); advance(); },
      previous() {
        requirePlaying();
        if (elapsed() - START > 5 || !S.history.length) { play(S.current); changed(); return "restart"; }
        advance("previous"); return "previous";
      },
      pause() { requirePlaying(); if (paused()) return false; S.pausedAt = now(); changed(); return true; },
      resume() {
        requirePlaying();
        if (!paused()) return false;
        S.pausedTotal += now() - S.pausedAt; S.pausedAt = null; changed(); return true;
      },
      remove(id) { if (!S.inVoice) throw new ControlError("not_in_voice"); const t = S.queue.splice(findQueued(id), 1)[0]; changed(); return t; },
      move(id, index) {
        if (!S.inVoice) throw new ControlError("not_in_voice");
        const i = findQueued(id);
        const to = Math.max(0, Math.min(S.queue.length - 1, index));
        S.queue.splice(to, 0, S.queue.splice(i, 1)[0]);
        changed();
      },
      jump(id) {
        requirePlaying();
        const i = findQueued(id);
        S.history.push(S.current); S.history = S.history.slice(-5);
        play(S.queue.splice(i, 1)[0]);
        changed();
        return S.current;
      },
      seek(position) {
        requirePlaying();
        const p = Math.max(START, Math.min(position, END - 1));
        const was = paused();
        play(S.current);
        S.startedAt = now() - (p - START);
        if (was) S.pausedAt = now();
        changed();
        return p;
      },
      crossfade(on) { S.crossfade = !!on; changed(); },
      add(url) {
        if (!S.inVoice) throw new ControlError("not_in_voice");
        queueLink(parseLink(url));
      },
      stop() {
        S.epoch++;
        S.queue = []; S.history = [];
        leaveVoice();
        if (!S.jobs) { S.loadLine = null; S.loadFinal = false; }
        setAction("⏹️ Stopped");
      },
    };
    const seekLabel = (applied) => {
      const rel = Math.floor(Math.max(0, applied - START));
      return `⏩ Seeked to ${Math.floor(rel / 60)}:${String(rel % 60).padStart(2, "0")}`;
    };

    // ---- status panel (sessions.render_panel)
    function renderPanel() {
      const lines = [];
      if (S.sid) lines.push(`📺 Watch the clip live: ${BASE_URL}/${S.sid}`);
      if (!S.inVoice) lines.push(`💤 Idle — ${PREFIX}play to resume`);
      else if (S.current) lines.push(`🎵 Now playing: **${short(S.current.title)}**` + (paused() ? " ⏸️ paused" : ""));
      else lines.push("🎵 Nothing playing yet");
      if (S.queue.length) {
        let line = `⏭️ Up next: ${short(S.queue[0].title)}`;
        if (S.queue.length > 1) line += ` and ${S.queue.length - 1} more`;
        lines.push(line);
      }
      if (S.crossfade) lines.push("🔀 Crossfade on");
      if (S.loadLine) lines.push(S.loadLine);
      else if (S.single) lines.push("📥 Downloading…");
      if (S.lastAction) lines.push(S.lastAction);
      return lines.join("\n");
    }

    // ---- web API (web.py), French errors as the real page expects
    const pub = (t) => ({ id: t.id, title: t.title, duration: t.duration, thumbnail: t.thumbnail, url: t.url });
    function state() {
      if (!S.sid) return { status: 404, body: { active: false } };
      const c = S.inVoice ? S.current : null;
      return {
        status: 200,
        body: {
          active: true, in_voice: S.inVoice, guild: "Friday Night", channel: "Lounge",
          loading: S.jobs > 0, paused: !!c && paused(), can_previous: !!c,
          crossfade: S.crossfade, crossfading: false, server_time: Date.now() / 1000,
          viewers: viewers(),
          theme: S.theme,
          now_playing: c ? Object.assign(pub(c), { video_url: c.video_url, elapsed: elapsed(), start: START, end: END }) : null,
          queue: S.inVoice ? S.queue.map(pub) : [],
        },
      };
    }
    // A few people on the page, slowly coming and going.
    const viewers = () => 3 + Math.round(1.5 + 1.5 * Math.sin(Date.now() / 47000));

    // ---- fake listening stats (stats.summary), built from the catalog with a
    // fixed seed so every visit shows the same numbers.
    function fakeStats(range) {
      const days = range === "week" ? 7 : range === "month" ? 30 : 120;
      let seed = 1337;
      const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
      const tracks = new Map(), artists = new Map(), djs = new Map(), skips = new Map();
      const heatmap = new Array(168).fill(0);
      const nowSec = Date.now() / 1000;
      let seconds = 0, count = 0;
      const bump = (m, k, n) => m.set(k, (m.get(k) || 0) + (n || 1));
      const plays = days * 9;
      for (let i = 0; i < plays; i++) {
        // Evenings and weekends are busier (UTC hour-of-week buckets).
        const day = Math.floor(rnd() * 7), weekend = day >= 4;
        const hour = rnd() < 0.75 ? 18 + Math.floor(rnd() * 6) : (weekend ? 13 : 12) + Math.floor(rnd() * 6);
        const k = Math.min(CATALOG.length - 1, Math.floor(Math.pow(rnd(), 1.6) * CATALOG.length));
        const skipped = rnd() < (k === 3 ? 0.35 : 0.08);
        heatmap[day * 24 + (hour % 24)]++;
        count++;
        seconds += skipped ? 12 : 200;
        bump(djs, rnd() < 0.15 ? null : DJS[Math.min(3, Math.floor(Math.pow(rnd(), 1.3) * 4))]);
        if (skipped) { bump(skips, k); continue; }
        bump(tracks, k);
        bump(artists, ARTISTS[k]);
      }
      const top = (m, n) => [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, n);
      const worst = top(skips, 1)[0];
      const weekDj = top(new Map([...djs].filter(([k]) => k !== null)), 1)[0];
      return {
        range,
        top_tracks: top(tracks, 10).map(([k, n]) => ({ title: CATALOG[k][0], artist: ARTISTS[k], video_id: CATALOG[k][1], plays: n })),
        top_artists: top(artists, 10).map(([a, n]) => ({ name: a, plays: n })),
        top_djs: top(djs, 5).map(([a, n]) => ({ name: a, plays: n })),
        dj_of_week: weekDj ? { name: weekDj[0], plays: Math.round(weekDj[1] * 7 / days) } : null,
        most_skipped: worst ? { title: CATALOG[worst[0]][0], artist: ARTISTS[worst[0]], skips: worst[1] } : null,
        totals: { hours: Math.round(seconds / 360) / 10, tracks: count, sessions: Math.round(days * 0.8) },
        heatmap,
        peak_listeners: { count: days > 7 ? 9 : 6, at: nowSec - (days > 7 ? 12 : 2) * 86400 - 3 * 3600 },
      };
    }

    const fail = (status, error) => ({ status, body: { ok: false, error } });
    function control(d) {
      d = d || {};
      const a = d.action;
      if (!S.sid) return fail(404, "Session inconnue ou expirée.");
      try {
        if (a === "next") { C.next(); setAction("⏭️ Skipped via the web page"); }
        else if (a === "previous") setAction(C.previous() === "restart" ? "⏮️ Restarted via the web page" : "⏮️ Previous track via the web page");
        else if (a === "pause") C.pause();
        else if (a === "resume") C.resume();
        else if (a === "remove") setAction(`🗑️ Removed: ${short(C.remove(d.id).title)}`);
        else if (a === "move") {
          const idx = parseInt(d.index, 10);
          if (!(idx >= 0)) return fail(400, "Position invalide.");
          C.move(d.id, idx); setAction("🔀 Queue reordered");
        } else if (a === "seek") {
          const p = Number(d.position);
          if (!isFinite(p) || p < 0) return fail(400, "Position invalide.");
          setAction(seekLabel(C.seek(p)));
        } else if (a === "jump") setAction(`▶️ Jumped to: ${short(C.jump(d.id).title)}`);
        else if (a === "crossfade") {
          if (typeof d.enabled !== "boolean") return fail(400, "Requête invalide.");
          C.crossfade(d.enabled);
        } else if (a === "add") {
          if (typeof d.url !== "string") return fail(400, "Lien manquant.");
          C.add(d.url);
          emit("web", { action: a });
          return { status: 202, body: { ok: true } };
        } else return fail(400, "Action inconnue.");
      } catch (e) {
        if (e instanceof ControlError) return fail(e.status, e.french);
        throw e;
      }
      emit("web", { action: a });
      return { status: 200, body: { ok: true } };
    }
    function setTheme(d) {
      d = d || {};
      const col = (v, f) => (/^#[0-9a-fA-F]{6}$/.test(v) ? v : f);
      S.theme = {
        skin: SKINS.includes(d.skin) ? d.skin : "arcade",
        base: PRESETS.includes(d.base) ? d.base : "nuit",
        bg: col(d.bg, S.theme.bg), text: col(d.text, S.theme.text), accent: col(d.accent, S.theme.accent),
        name: String(d.name || "").slice(0, 32), logo_url: null,
      };
      setAction("🎨 Clip page theme updated via the web page");
      return { status: 200, body: { ok: true, theme: S.theme } };
    }
    /** Fake HTTP: `route` is state | stats | control | theme | theme/logo. */
    function http(route, method, body) {
      if (route === "state") return state();
      if (route === "stats") {
        if (!S.sid) return { status: 404, body: { active: false } };
        const range = body && body.range;
        if (!["week", "month", "all"].includes(range)) return fail(400, "Période inconnue.");
        return { status: 200, body: fakeStats(range) };
      }
      if (route === "control") return control(body);
      if (!S.sid) return fail(404, "Session inconnue ou expirée.");
      if (route === "theme") return setTheme(body);
      return fail(400, "Le logo n'est pas disponible dans la démo.");
    }

    if (standalone) {
      S.sid = newSid();
      S.inVoice = true;
      while (S.queue.length < 4) S.queue.push(mkTrack(rot++));
      play(S.queue.shift());
      S.queue.push(mkTrack(rot++));
    }

    return {
      on, http, parseLink, queueLink, join, openSession, renderPanel, seekLabel, C, ControlError,
      catalog: CATALOG, start: START, prefix: PREFIX, presets: PRESETS, skins: SKINS, baseUrl: BASE_URL,
      get sid() { return S.sid; },
      get inVoice() { return S.inVoice; },
      get playing() { return S.inVoice && !!S.current && !paused(); },
      get current() { return S.current; },
      get queue() { return S.queue.slice(); },
      get jobs() { return S.jobs; },
      get theme() { return S.theme; },
      setSkin(skin) { S.theme = Object.assign({}, S.theme, { skin }); changed(); },
      setAction,
    };
  }

  window.DJDemoBackend = { create, parseLink, ControlError };
})();
