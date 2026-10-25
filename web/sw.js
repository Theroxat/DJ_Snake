/* DJ Snake service worker. Caches the app shell only: the page HTML (one
 * copy, the same for every session) and the Google Fonts. It never touches
 * the API, the live stream (/api/.../events), media files, lyrics or theme
 * logos: those are not intercepted at all, so they always hit the network.
 * "__VERSION__" is replaced by the server with the app version, so a release
 * starts a fresh cache and the activate step deletes the old ones. */
"use strict";

const VERSION = "__VERSION__";
const CACHE = "djsnake-shell-" + VERSION;
const SHELL_KEY = "/__shell"; // never a session id: the page is the same for all
const NAV_TIMEOUT_MS = 2500; // slower than this, a cached shell opens at once
const FONT_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com"];

const OFFLINE = {
  en: {
    title: "Session offline",
    text: "The bot is unreachable. Check your connection or try again in a moment.",
    retry: "Try again",
  },
  fr: {
    title: "Session hors ligne",
    text: "Le bot est injoignable. Vérifiez votre connexion ou réessayez dans un instant.",
    retry: "Réessayer",
  },
};

function offlinePage() {
  const l = String(self.navigator.language || "en").toLowerCase().startsWith("fr") ? "fr" : "en";
  const o = OFFLINE[l];
  const html =
    '<!doctype html><html lang="' + l + '"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    "<title>" + o.title + "</title><style>" +
    ":root{color-scheme:dark light}" +
    "body{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;" +
    "box-sizing:border-box;font:16px/1.5 system-ui,sans-serif;background:#0a0a0a;color:#f3e7cf}" +
    "main{max-width:26rem;text-align:center}h1{margin:0 0 .5rem;font-size:1.5rem}" +
    "button{font:inherit;font-weight:700;padding:.6rem 1.2rem;border:2px solid currentColor;" +
    "background:none;color:inherit;cursor:pointer;border-radius:6px}" +
    "button:focus-visible{outline:3px solid #00d4ff;outline-offset:2px}" +
    "@media (prefers-color-scheme:light){body{background:#f1f2f5;color:#1f2430}}" +
    "</style></head><body><main role=\"alert\"><h1>" + o.title + "</h1><p>" + o.text +
    '</p><button type="button" onclick="location.reload()">' + o.retry + "</button></main>" +
    '<script>addEventListener("online",()=>location.reload())</script></body></html>';
  return new Response(html, {
    status: 503,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}

self.addEventListener("install", (event) => {
  // Nothing to precache: the shell is stored on first load. Taking over at
  // once is safe because the worker only serves the shell and fonts.
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("djsnake-shell-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// /{sid} and nothing else: not /sw.js, /healthz, /metrics or any deeper path.
function isPage(url) {
  return /^\/[^/]+$/.test(url.pathname) && !["/sw.js", "/healthz", "/metrics"].includes(url.pathname);
}

async function navigate(request) {
  const cache = await caches.open(CACHE);
  const network = fetch(request).then(async (res) => {
    const type = res.headers.get("Content-Type") || "";
    // Only the real page: an error or a proxy page is never kept.
    if (res.ok && type.includes("text/html")) {
      await cache.put(SHELL_KEY, res.clone());
    }
    return res;
  });
  const cached = await cache.match(SHELL_KEY);
  try {
    if (!cached) return await network;
    // Fast reopen on a slow link: a stale shell is harmless, the page asks
    // the bot for the state itself (an ended session shows as expired).
    return await Promise.race([
      network,
      new Promise((resolve) => setTimeout(() => resolve(cached), NAV_TIMEOUT_MS)),
    ]);
  } catch (e) {
    network.catch(() => {});
    return offlinePage();
  }
}

async function font(request) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(request);
  const fresh = fetch(request)
    .then((res) => {
      if (res.ok || res.type === "opaque") cache.put(request, res.clone());
      return res;
    })
    .catch(() => null);
  return hit || (await fresh) || Response.error();
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin === self.location.origin) {
    if (request.mode === "navigate" && isPage(url)) event.respondWith(navigate(request));
    return; // everything else (API, events, media, lyrics, logos) goes to the network
  }
  if (FONT_HOSTS.includes(url.hostname)) event.respondWith(font(request));
});
