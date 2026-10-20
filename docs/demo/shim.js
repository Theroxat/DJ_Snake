/* DJ Snake demo shim. Replaces window.fetch for the bot's web API so the real
 * clip page (web/index.html, built into player.html) runs on static hosting.
 * Intercepted: GET /api/{sid}/state, GET /api/{sid}/stats, POST /api/{sid}/control,
 * POST /api/{sid}/theme, POST|DELETE /api/{sid}/theme/logo.
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

  // `?edit=demo` opens the theme editor: give the page a well-formed token.
  try {
    const p = new URLSearchParams(location.search);
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

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    const m = /\/api\/[^/]+\/(state|stats|control|theme(?:\/logo)?)(?:\?|$)/.exec(url);
    if (!m) return realFetch(input, init);
    const method = ((init && init.method) || "GET").toUpperCase();
    let body = null;
    try { body = init && typeof init.body === "string" ? JSON.parse(init.body) : null; } catch (e) {}
    if (m[1] === "stats") {
      try { body = { range: new URL(url, location.href).searchParams.get("range") || "week" }; } catch (e) {}
    }
    await new Promise((r) => setTimeout(r, 40));
    // JSON round trip: the answer comes from the parent window's realm.
    const res = backend.http(m[1], method, body);
    return new Response(JSON.stringify(res.body), {
      status: res.status, headers: { "Content-Type": "application/json" },
    });
  };
})();
