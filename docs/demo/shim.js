/* DJ Snake demo shim. Replaces window.fetch for the bot's web API so the real
 * clip page (web/index.html, built into player.html) runs on static hosting.
 * Intercepted: GET /api/{sid}/state, GET /api/{sid}/stats, POST /api/{sid}/control,
 * POST /api/{sid}/theme, POST|DELETE /api/{sid}/theme/logo,
 * GET /api/{sid}/lyrics/{id}, and the live update stream
 * GET /api/{sid}/events (a fake EventSource).
 * Embedded in the Discord demo (index.html), it talks to the wrapper's fake
 * bot (window.parent.DJDemoBackend.shared), so both sides share one session;
 * opened on its own, it runs a standalone fake bot that is always playing and
 * now and then loads a fake playlist (the "Downloading" list). */
(() => {
  "use strict";

  let backend = null, embedded = false;
  try {
    if (window.parent !== window && window.parent.DJDemoBackend && window.parent.DJDemoBackend.shared) {
      backend = window.parent.DJDemoBackend.shared;
      embedded = true;
    }
  } catch (e) {}
  if (!backend) backend = window.DJDemoBackend.create({ standalone: true });
  if (embedded) document.documentElement.classList.add("demo-embedded");

  // `?view` plays the view-only link: the fake API answers as web.py does
  // for a view-only id (no controls, 403 on control).
  let view = false;
  // `?edit=demo` opens the theme editor: give the page a well-formed token.
  try {
    const p = new URLSearchParams(location.search);
    view = p.has("view");
    if (p.get("edit") === "demo") {
      p.set("edit", "demo-edit-token-0000");
      history.replaceState(null, "", location.pathname + "?" + p.toString());
    }
  } catch (e) {}

  // Standalone, nobody adds anything: load a fake playlist now and then so
  // the "Downloading" list shows how downloads queue up.
  if (!embedded && backend.demoLoad) {
    setTimeout(() => backend.demoLoad(5), 1500);
    setInterval(() => backend.demoLoad(4), 45000);
  }

  // A real YouTube link as the example in the add field.
  document.addEventListener("DOMContentLoaded", () => {
    const f = document.getElementById("addurl");
    if (f) f.placeholder = "https://www.youtube.com/watch?v=" + backend.catalog[0][1];
  });

  // Live updates (GET /api/{sid}/events): a fake EventSource fed by the fake
  // bot's change events, coalesced like the server's stream, plus a state
  // every 5 s standing in for the server's state diff (viewers come and go).
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.onopen = this.onmessage = this.onerror = null;
      this._timer = null;
      this._off = backend.on((ev) => {
        if (ev.type === "change" || ev.type === "voice") this._queue();
      });
      this._tick = setInterval(() => this._queue(), 5000);
      this._timer = setTimeout(() => this._send(), 40);
    }
    addEventListener() {} // `ping`: the 5 s states keep the page's liveness check happy
    _queue() { if (!this._timer) this._timer = setTimeout(() => this._send(), 100); }
    _send() {
      this._timer = null;
      if (this.readyState === 2) return;
      this.readyState = 1;
      // JSON round trip: the answer comes from the parent window's realm.
      const res = backend.http("state", "GET", null, view);
      const data = JSON.stringify(res.status === 200 ? res.body : { active: false });
      if (this.onmessage) this.onmessage({ data });
      if (res.status !== 200) this.close(); // the server ends the stream too
    }
    close() {
      this.readyState = 2;
      this._off();
      clearInterval(this._tick);
      clearTimeout(this._timer);
    }
  }
  window.EventSource = FakeEventSource;

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    const m = /\/api\/[^/]+\/(state|stats|control|playlists|theme(?:\/logo)?|lyrics\/[^/?]+)(?:\?|$)/.exec(url);
    if (!m) return realFetch(input, init);
    const method = ((init && init.method) || "GET").toUpperCase();
    let body = null;
    try { body = init && typeof init.body === "string" ? JSON.parse(init.body) : null; } catch (e) {}
    if (m[1] === "stats") {
      try { body = { range: new URL(url, location.href).searchParams.get("range") || "week" }; } catch (e) {}
    }
    await new Promise((r) => setTimeout(r, 40));
    // JSON round trip: the answer comes from the parent window's realm.
    const res = backend.http(m[1], method, body, view);
    return new Response(JSON.stringify(res.body), {
      status: res.status, headers: { "Content-Type": "application/json" },
    });
  };
})();
