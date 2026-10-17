/* DJ Snake demo shim. Fakes the bot's web API in the browser so the real clip
 * page (web/index.html) runs on static hosting. Footage: CC-BY Blender Foundation / Blender Studio films (see docs/README.md).
 * Intercepted: GET /api/{sid}/state, POST /api/{sid}/control,
 * POST /api/{sid}/theme, POST|DELETE /api/{sid}/theme/logo. */
(() => {
  "use strict";

  // `?edit=demo` opens the theme editor: give the page a well-formed token.
  try {
    const p = new URLSearchParams(location.search);
    if (p.get("edit") === "demo") {
      p.set("edit", "demo-edit-token-0000");
      history.replaceState(null, "", location.pathname + "?" + p.toString());
    }
  } catch (e) {}

  const now = () => performance.now() / 1000;
  const media = (n) => new URL("media/clip" + n + ".webm", document.baseURI).href;
  const thumb = (n) => new URL("media/thumb" + n + ".jpg", document.baseURI).href;

  // Real Creative Commons Attribution films (credits: docs/README.md). Each
  // clip is a 25 s excerpt of the YouTube video at `url`; titles are the
  // YouTube titles. The page shows the excerpt length as the duration.
  const CATALOG = [
    ["Caminandes 3: Llamigos", "SkVqJ1SGeL0"],
    ["Cosmos Laundromat - First Cycle. Official Blender Foundation release.", "Y-rmzh0PI3c"],
    ["Big Buck Bunny 60fps 4K - Official Blender Foundation Short Film", "aqz-KE-bpKQ"],
    ["Tears of Steel - Blender VFX Open Movie", "R6MlUcmOul8"],
    ["Sintel - Open Movie by Blender Foundation", "eRsGyueVLvQ"],
  ];
  const DURATION = 25, START = 1, END = 24;
  let seq = 0;
  const mkTrack = (i) => {
    const k = i % CATALOG.length;
    const c = CATALOG[k];
    seq++;
    return {
      id: "demo" + seq,
      title: c[0],
      duration: DURATION,
      thumbnail: thumb(k + 1),
      url: "https://www.youtube.com/watch?v=" + c[1],
      video_url: media(k + 1),
    };
  };

  // Real YouTube link as example in the add field.
  document.addEventListener("DOMContentLoaded", () => {
    const f = document.getElementById("addurl");
    if (f) f.placeholder = "https://www.youtube.com/watch?v=" + CATALOG[0][1];
  });

  const S = {
    queue: [], history: [], current: null,
    startedAt: 0, pausedAt: null, pausedTotal: 0, paused: false,
    crossfade: false, nextIdx: 0,
    theme: { skin: "arcade", base: "nuit", bg: "#101820", text: "#f2f2f2", accent: "#ffb703", name: "", logo_url: null },
  };
  const take = () => mkTrack(S.nextIdx++);
  const play = (t) => { S.current = t; S.startedAt = now(); S.pausedAt = null; S.pausedTotal = 0; S.paused = false; };
  const fill = () => { while (S.queue.length < 4) S.queue.push(take()); };
  fill();
  play(S.queue.shift());
  fill();

  const elapsed = () =>
    START + Math.max(0, (S.paused ? S.pausedAt : now()) - S.startedAt - S.pausedTotal);
  function advance() {
    if (S.current) S.history.push(S.current);
    S.history = S.history.slice(-5);
    fill();
    play(S.queue.shift());
    fill();
  }
  function tickEnd() { if (!S.paused && elapsed() >= END) advance(); }
  setInterval(tickEnd, 500);

  const json = (obj, status) =>
    new Response(JSON.stringify(obj), { status: status || 200, headers: { "Content-Type": "application/json" } });
  const err = (msg, status) => json({ ok: false, error: msg }, status || 400);
  const pub = (t) => ({ id: t.id, title: t.title, duration: t.duration, thumbnail: t.thumbnail, url: t.url });

  function state() {
    tickEnd();
    const c = S.current;
    return {
      active: true, in_voice: true, guild: "Serveur démo", channel: "musique", loading: false,
      paused: S.paused, can_previous: true, crossfade: S.crossfade, crossfading: false,
      server_time: Date.now() / 1000, theme: S.theme,
      now_playing: Object.assign(pub(c), { video_url: c.video_url, elapsed: elapsed(), start: START, end: END }),
      queue: S.queue.map(pub),
    };
  }

  function control(d) {
    const a = d.action;
    if (a === "next") advance();
    else if (a === "previous") {
      if (elapsed() - START > 5 || !S.history.length) { play(S.current); }
      else { S.queue.unshift(S.current); play(S.history.pop()); }
    } else if (a === "pause") {
      if (!S.paused) { S.paused = true; S.pausedAt = now(); }
    } else if (a === "resume") {
      if (S.paused) { S.pausedTotal += now() - S.pausedAt; S.paused = false; S.pausedAt = null; }
    } else if (a === "remove") {
      const i = S.queue.findIndex((t) => t.id === d.id);
      if (i < 0) return err("Morceau introuvable.", 404);
      S.queue.splice(i, 1); fill();
    } else if (a === "move") {
      const i = S.queue.findIndex((t) => t.id === d.id);
      if (i < 0) return err("Morceau introuvable.", 404);
      const to = Math.max(0, Math.min(S.queue.length - 1, parseInt(d.index, 10) || 0));
      S.queue.splice(to, 0, S.queue.splice(i, 1)[0]);
    } else if (a === "jump") {
      const i = S.queue.findIndex((t) => t.id === d.id);
      if (i < 0) return err("Morceau introuvable.", 404);
      S.history.push(S.current);
      play(S.queue.splice(i, 1)[0]); fill();
    } else if (a === "seek") {
      const p = Math.max(START, Math.min(Number(d.position) || START, END - 1));
      const wasPaused = S.paused;
      play(S.current);
      S.startedAt = now() - (p - START);
      if (wasPaused) { S.paused = true; S.pausedAt = now(); }
    } else if (a === "crossfade") {
      S.crossfade = !!d.enabled;
    } else if (a === "add") {
      if (!/^https?:\/\/(www\.|m\.|music\.)?(youtube\.com|youtu\.be)\//i.test(String(d.url || "")))
        return err("Seuls les liens YouTube sont acceptés.", 400);
      S.queue.push(take());
      return json({ ok: true }, 202);
    } else return err("Action inconnue.", 400);
    return json({ ok: true });
  }

  function theme(d) {
    const col = (v, f) => (/^#[0-9a-fA-F]{6}$/.test(v) ? v : f);
    const skins = ["arcade", "minimal", "nuit", "vinyle", "club", "terminal", "perso"];
    const bases = skins.slice(0, 6);
    S.theme = {
      skin: skins.includes(d.skin) ? d.skin : "arcade",
      base: bases.includes(d.base) ? d.base : "nuit",
      bg: col(d.bg, S.theme.bg), text: col(d.text, S.theme.text), accent: col(d.accent, S.theme.accent),
      name: String(d.name || "").slice(0, 32), logo_url: null,
    };
    return json({ ok: true, theme: S.theme });
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    const m = /\/api\/[^/]+\/(state|control|theme(?:\/logo)?)(?:\?|$)/.exec(url);
    if (!m) return realFetch(input, init);
    const method = ((init && init.method) || "GET").toUpperCase();
    await new Promise((r) => setTimeout(r, 40));
    if (m[1] === "state") return json(state());
    if (m[1] === "control") {
      let d = {};
      try { d = JSON.parse(init.body); } catch (e) {}
      return control(d);
    }
    if (m[1] === "theme") {
      let d = {};
      try { d = JSON.parse(init.body); } catch (e) {}
      return theme(d);
    }
    return err("Le logo n'est pas disponible dans la démo.", 400);
  };
})();
