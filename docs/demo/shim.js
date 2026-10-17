/* DJ Snake demo shim. Fakes the bot's web API in the browser so the real clip
 * page (web/index.html) runs on static hosting. All data is fictional.
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
  const thumb = (a, b) =>
    "data:image/svg+xml," +
    encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 9"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="' +
        a + '"/><stop offset="1" stop-color="' + b + '"/></linearGradient></defs><rect width="16" height="9" fill="url(#g)"/></svg>'
    );
  const media = (n) => new URL("media/clip" + n + ".webm", document.baseURI).href;

  const CATALOG = [
    ["Midnight Circuit", "#ff2e63", "#252a34"],
    ["Velvet Static", "#6a11cb", "#2575fc"],
    ["Paper Lanterns", "#f6d365", "#fda085"],
    ["Low Tide Radio", "#00f5a0", "#00d9f5"],
    ["Electric Garden", "#ff512f", "#dd2476"],
    ["Glass Highway", "#08d9d6", "#252a34"],
  ];
  const DURATION = 60, START = 2, END = 58;
  let seq = 0;
  const mkTrack = (i) => {
    const c = CATALOG[i % CATALOG.length];
    seq++;
    return {
      id: "demo" + seq,
      title: c[0] + " (demo)",
      duration: DURATION,
      thumbnail: thumb(c[1], c[2]),
      url: "https://www.youtube.com/watch?v=demo" + String(seq).padStart(7, "0"),
      video_url: media((i % 5) + 1),
    };
  };

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
      const t = take();
      t.title = "Piste ajoutée " + seq + " (demo)";
      S.queue.push(t);
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
