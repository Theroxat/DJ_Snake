/* DJ Snake demo: the mock Discord client. It owns the shared fake bot
 * (backend.js) that the clip page iframe (player.html + shim.js) also uses,
 * answers commands like djsnake/commands.py, keeps the status panel edited in
 * place like djsnake/sessions.py, and runs the guided tour. */
(() => {
  "use strict";

  const B = window.DJDemoBackend.shared = window.DJDemoBackend.create({});
  const $ = (id) => document.getElementById(id);
  const later = (ms) => new Promise((r) => setTimeout(r, ms));
  const P = B.prefix;
  const PLAY_CMD = P + "play https://open.spotify.com/playlist/0DjSnakeDemoPlaylist00";
  const MOBILE = window.matchMedia("(max-width: 900px)");

  // ---- people
  const PEOPLE = {
    you: { name: "you", initials: "YO", color: "#5865f2", nameColor: "#f2f3f5" },
    maya: { name: "Maya", initials: "MA", color: "#c2185b", nameColor: "#f47fff" },
    theo: { name: "Theo", initials: "TH", color: "#00796b", nameColor: "#1abc9c" },
    jun: { name: "Jun", initials: "JU", color: "#b26a00", nameColor: "#f1c40f" },
    bot: { name: "DJ Snake", initials: "🐍", color: "", nameColor: "#2ecc71", bot: true },
    tip: { name: "Demo tip", initials: "💡", color: "", nameColor: "#b5bac1", tip: true },
  };
  function avatar(p, size) {
    const d = document.createElement("div");
    d.className = "av" + (size ? " s" + size : "") + (p.bot ? " bot" : "") + (p.tip ? " tip" : "");
    if (p.color) d.style.background = p.color;
    d.textContent = p.initials;
    d.setAttribute("aria-hidden", "true");
    return d;
  }

  // ---- voice channel (sidebar list + compact strip on phones)
  const VOICE = ["you", "maya", "theo", "jun"];
  let botJoinedAt = 0;
  function renderVoice() {
    const members = (B.inVoice ? ["bot"] : []).concat(VOICE);
    const speaking = B.playing;
    for (const [box, size] of [[$("vlist"), 24], [$("vstrip"), 24]]) {
      box.querySelectorAll(".vm").forEach((n) => n.remove());
      for (const key of members) {
        const p = PEOPLE[key];
        const li = document.createElement(box.tagName === "UL" ? "li" : "div");
        li.className = "vm" + (key === "bot" && speaking ? " speaking" : "") + (key === "bot" && performance.now() - botJoinedAt < 400 ? " joining" : "");
        li.title = p.name + (key === "bot" && speaking ? " (speaking)" : "");
        li.appendChild(avatar(p, size));
        const n = document.createElement("span");
        n.className = "name";
        n.textContent = p.name;
        li.appendChild(n);
        if (p.bot) { const t = document.createElement("span"); t.className = "tag"; t.textContent = "Bot"; li.appendChild(t); }
        box.appendChild(li);
      }
    }
  }
  let lastVoiceSig = "";
  function updateVoice() {
    const sig = B.inVoice + "/" + B.playing;
    if (sig === lastVoiceSig) return;
    lastVoiceSig = sig;
    renderVoice();
  }

  // ---- messages
  const msgs = $("msgs");
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  /** A small subset of Discord markdown: code blocks, inline code, bold, links. */
  function md(text) {
    const parts = String(text).split(/```\n?([\s\S]*?)```/);
    return parts.map((part, i) => {
      if (i % 2) return "<pre>" + esc(part.replace(/\n$/, "")) + "</pre>";
      return part.split(/`([^`]+)`/).map((p, j) => {
        if (j % 2) return "<code>" + esc(p) + "</code>";
        let h = esc(p).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
        h = h.replace(/https?:\/\/[^\s<]+/g, (u) => {
          if (u.startsWith(B.baseUrl + "/")) return '<a class="clip" href="#" data-clip="1">' + u + "</a>";
          return '<span class="lnk">' + u + "</span>";
        });
        return h;
      }).join("");
    }).join("");
  }
  const clock = () => new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  let lastAuthor = null, lastAt = 0;
  function nearBottom() { return msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 120; }
  function scrollDown(force) { if (force || nearBottom()) msgs.scrollTop = msgs.scrollHeight; }
  /** Post a message; returns its element. */
  function post(who, text, opts) {
    opts = opts || {};
    const stick = nearBottom();
    const p = PEOPLE[who];
    const el = document.createElement("div");
    const cont = !opts.eph && lastAuthor === who && Date.now() - lastAt < 5 * 60 * 1000;
    el.className = "msg" + (cont ? " cont" : "") + (opts.eph ? " eph" : "") + (opts.cls ? " " + opts.cls : "");
    el.appendChild(avatar(p));
    const body = document.createElement("div");
    body.className = "mbody";
    if (!cont) {
      const head = document.createElement("div");
      head.className = "mhead";
      head.innerHTML = '<span class="name"></span>' + (p.bot ? '<span class="tag">Bot</span>' : "") + "<time></time>";
      head.querySelector(".name").textContent = p.name;
      head.querySelector(".name").style.color = p.nameColor;
      head.querySelector("time").textContent = (opts.time || "Today at " + clock());
      body.appendChild(head);
    }
    const t = document.createElement("div");
    t.className = "mtext";
    t.innerHTML = md(text);
    body.appendChild(t);
    if (opts.eph) {
      const f = document.createElement("div");
      f.className = "foot";
      f.textContent = "👁 Only you can see this · demo hint";
      body.appendChild(f);
    }
    const r = document.createElement("div");
    r.className = "reacts";
    body.appendChild(r);
    el.appendChild(body);
    msgs.appendChild(el);
    lastAuthor = opts.eph ? null : who;
    lastAt = Date.now();
    scrollDown(stick || who === "you");
    return el;
  }
  function react(el, emoji) {
    const r = document.createElement("span");
    r.className = "react";
    r.title = "DJ Snake reacted";
    r.innerHTML = esc(emoji || "✅") + "<b>1</b>";
    el.querySelector(".reacts").appendChild(r);
    scrollDown();
  }
  async function botSay(text) {
    $("typing").innerHTML = "<b>DJ Snake</b> is typing…";
    await later(450);
    $("typing").textContent = "";
    return post("bot", text);
  }
  const tip = (text) => post("tip", text, { eph: true });

  // ---- status panel: one message, edited in place (sessions.refresh_panel)
  let panelEl = null, panelText = "", panelTimer = null, panelLastEdit = 0;
  const PANEL_MIN_INTERVAL = 900; // ms; the bot waits 1.5 s between edits
  function postPanel() {
    const old = panelEl;
    clearTimeout(panelTimer); panelTimer = null;
    panelText = B.renderPanel();
    lastAuthor = null; // a fresh panel always shows its header
    panelEl = post("bot", panelText, { cls: "panel" });
    panelLastEdit = performance.now();
    if (old) old.remove();
    tourPanelPosted();
  }
  function writePanel() {
    panelTimer = null;
    if (!panelEl) return;
    const text = B.renderPanel();
    if (text === panelText) return;
    const stick = nearBottom();
    panelText = text;
    panelLastEdit = performance.now();
    const t = panelEl.querySelector(".mtext");
    t.innerHTML = md(text) + '<span class="edited">(edited)</span>';
    panelEl.classList.add("flash");
    requestAnimationFrame(() => requestAnimationFrame(() => panelEl && panelEl.classList.remove("flash")));
    if (stick) scrollDown(true);
  }
  function refreshPanel() {
    if (!panelEl || panelTimer) return;
    const wait = Math.max(120, panelLastEdit + PANEL_MIN_INTERVAL - performance.now());
    panelTimer = setTimeout(writePanel, wait);
  }

  // ---- the clip page iframe
  let frame = null, frameNotify = 0;
  function notifyFrame() {
    // The page gets pushed states (the shim's fake EventSource); should it
    // fall back to polling, it polls at once on visibilitychange, so a
    // Discord command still shows up on the page right away.
    if (!frame || frameNotify) return;
    frameNotify = setTimeout(() => {
      frameNotify = 0;
      try { frame.contentDocument.dispatchEvent(new Event("visibilitychange")); } catch (e) {}
    }, 150);
  }
  function showView(web) {
    document.body.classList.toggle("view-web", !!web);
    tourPlace();
  }
  function openClip(query) {
    if (!B.sid) return;
    $("urltext").innerHTML = '<span class="host">dj.example.com</span>/' + esc(B.sid) + (query ? esc(query) : "");
    if (!frame || query !== undefined) {
      if (frame) frame.remove();
      frame = document.createElement("iframe");
      frame.title = "DJ Snake clip page";
      frame.setAttribute("allow", "fullscreen; autoplay");
      frame.src = "player.html" + (query || "");
      frame.addEventListener("load", tourFrameLoaded, { once: true });
      $("ph").hidden = true;
      $("wbody").appendChild(frame);
    }
    document.body.classList.add("has-web");
    showView(true);
  }
  msgs.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-clip]");
    if (!a) return;
    e.preventDefault();
    openClip();
  });
  $("back").addEventListener("click", () => showView(false));
  $("toweb").addEventListener("click", () => showView(true));
  $("themelink").addEventListener("click", (e) => {
    e.preventDefault();
    B.openSession(); // as !theme edit: opens a session (and its panel) if none
    openClip("?edit=demo");
  });

  // ---- backend events
  B.on((ev) => {
    if (ev.type === "panel-post") postPanel();
    else if (ev.type === "voice") {
      if (ev.inVoice) {
        botJoinedAt = performance.now();
        if (tour.step === "free") $("input").placeholder = "Message #bot-commands";
      }
      lastVoiceSig = "";
      updateVoice();
    }
    else if (ev.type === "web") tourDone("web");
    if (ev.type === "change" || ev.type === "voice") { refreshPanel(); updateVoice(); notifyFrame(); }
  });

  // ---- commands (commands.py)
  function parsePosition(text) {
    const parts = String(text).split(":").map(Number);
    if (!parts.length || parts.length > 3 || parts.some((p) => !isFinite(p) || p < 0) || String(text).trim() === "") return null;
    return parts.reduce((s, p) => s * 60 + p, 0);
  }
  /** _run_command_control: run a control, answer its error or react. */
  async function runControl(msg, fn) {
    if (!B.sid) { await botSay("❌ No music is currently playing."); return false; }
    let result;
    try { result = fn(); } catch (e) {
      if (e instanceof B.ControlError) { await botSay("❌ " + e.message); return false; }
      throw e;
    }
    await later(250);
    react(msg);
    return { result };
  }
  const HELP = "```\n​No Category:\n  crossfade\n  help      Shows this message\n  list\n  next\n  pause\n  play\n" +
    "  reset     Force reset the voice connection\n  resume\n  seek\n  stop\n" +
    "  theme     Show or set the clip page's default theme for this server.\n\n" +
    `Type ${P}help command for more info on a command.\nYou can also type ${P}help category for more info on a category.\n` + "```";

  const COMMANDS = {
    async play(msg, arg) {
      if (!arg) return botSay(`❌ Usage: \`${P}play <YouTube or Spotify link>\``);
      let link;
      try { link = B.parseLink(arg); } catch (e) {
        if (e instanceof B.ControlError) return botSay("❌ " + e.message);
        throw e;
      }
      tourDone("send");
      await later(300);
      react(msg);
      if (!B.inVoice) { await later(600); B.join(); }
      B.queueLink(link);
    },
    async next(msg) { if (await runControl(msg, () => B.C.next())) B.setAction("⏭️ Skipped"); },
    async pause(msg) { if (await runControl(msg, () => B.C.pause())) refreshPanel(); },
    async resume(msg) { if (await runControl(msg, () => B.C.resume())) refreshPanel(); },
    async seek(msg, arg) {
      if (!arg) return botSay(`❌ Missing argument. Try \`${P}help\`.`);
      const s = parsePosition(arg);
      if (s === null) return botSay("❌ Invalid position. Use seconds or mm:ss.");
      const r = await runControl(msg, () => B.C.seek(B.start + s));
      if (r) B.setAction(B.seekLabel(r.result));
    },
    async crossfade(msg, arg) {
      const mode = (arg || "").toLowerCase();
      if (mode !== "on" && mode !== "off") return botSay(`❌ Usage: \`${P}crossfade on|off\``);
      B.C.crossfade(mode === "on");
      await later(250);
      react(msg);
    },
    async stop(msg) {
      if (!B.sid) return botSay("❌ No music is currently playing.");
      B.C.stop();
      await later(250);
      react(msg);
    },
    async reset() {
      await botSay("🔧 Resetting voice connection...");
      if (B.sid) { B.C.stop(); B.setAction(null); }
      await later(1500);
      await botSay("✅ Voice connection reset!");
    },
    async list() {
      const q = B.inVoice ? B.queue : [];
      if (!q.length) return botSay("❌ The queue is empty.");
      let m = "🎶 **Queue:**\n" + q.slice(0, 10).map((t, i) => `${i + 1}. ${t.title}`).join("\n");
      if (q.length > 10) m += `\n... and ${q.length - 10} more tracks`;
      return botSay(m);
    },
    async help() { return botSay(HELP); },
    async theme(msg, arg) {
      const choice = (arg || "").trim().toLowerCase();
      if (!choice) {
        return botSay(`🎨 Clip page theme: **${B.theme.skin}** (server default)\nPresets: ${B.skins.join(", ")}\n` +
          `\`${P}theme <preset>\` sets it, \`${P}theme reset\` restores \`arcade\`, ` +
          `\`${P}theme edit\` sends you an editor link (custom colors, name, logo).`);
      }
      if (choice === "edit") {
        await later(250); react(msg);
        tip("The real bot sends the editor link by DM. Here it opens in the clip page panel.");
        B.openSession();
        openClip("?edit=demo");
        return;
      }
      const skin = choice === "reset" ? "arcade" : choice;
      if (!B.skins.includes(skin)) return botSay(`❌ Unknown theme. Presets: ${B.skins.join(", ")}, or \`reset\` / \`edit\`.`);
      B.setSkin(skin);
      await later(250); react(msg);
      if (B.sid) B.setAction(`🎨 Clip page theme: ${skin}`);
    },
  };

  let busy = Promise.resolve();
  async function handle(text) {
    const msg = post("you", text);
    if (tour.step === "try") tourDone("cmd");
    if (!text.startsWith(P)) {
      await later(500);
      tip(`DJ Snake only reads commands that start with \`${P}\`. Try \`${P}help\`, or \`${P}play <link>\`.`);
      return;
    }
    const [name, ...rest] = text.slice(P.length).trim().split(/\s+/);
    const cmd = COMMANDS[(name || "").toLowerCase()];
    if (!cmd) {
      await later(500);
      tip(`\`${P}${name || ""}\` is not a command, so the real bot stays silent. Try \`${P}help\`, \`${P}next\`, \`${P}pause\`, \`${P}list\` or \`${P}play <link>\`.`);
      return;
    }
    await cmd(msg, rest.join(" "));
  }
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    busy = busy.then(() => handle(text)).catch((err) => console.error(err)).then(() => {
      // While the tour waits for the first !play, offer it again.
      if (tour.step === "send" && !input.value) input.value = PLAY_CMD;
      if (!B.inVoice && B.sid && !input.value && tour.step === "free") input.placeholder = `Message #bot-commands (try ${P}play)`;
    });
  });

  // ---- guided tour
  const tour = { step: "send", bubbles: {} };
  function bubble(key, html, target, side, label) {
    hideBubble(key);
    const b = document.createElement("div");
    b.className = "bubble";
    b.setAttribute("role", "note");
    b.innerHTML = (label ? '<span class="step">' + label + "</span>" : "") + html +
      '<button class="x" type="button" aria-label="Dismiss">×</button>';
    b.querySelector(".x").addEventListener("click", () => { hideBubble(key); if (!Object.keys(tour.bubbles).length && tour.step === "try") endTour(); });
    document.body.appendChild(b);
    tour.bubbles[key] = { el: b, target, side };
    tourPlace();
  }
  function hideBubble(key) {
    const b = tour.bubbles[key];
    if (b) { b.el.remove(); delete tour.bubbles[key]; }
  }
  function tourPlace() {
    const vw = window.innerWidth, vh = window.innerHeight;
    for (const key in tour.bubbles) {
      const { el, target, side } = tour.bubbles[key];
      const t = target();
      const r = t && t.getBoundingClientRect();
      if (!r || (!r.width && !r.height) || !t.offsetParent || r.bottom < 0 || r.top > vh) { el.style.visibility = "hidden"; continue; }
      el.style.visibility = "";
      const w = el.offsetWidth, h = el.offsetHeight;
      let left, top;
      el.classList.remove("up", "down", "flat");
      if (side === "inside") {
        left = r.left + (r.width - w) / 2;
        top = r.top + 14;
        el.classList.add("flat");
      } else {
        left = Math.min(r.left + 8, r.right - w);
        top = side === "top" ? r.top - h - 12 : r.bottom + 12;
        el.classList.add(side === "top" ? "up" : "down");
      }
      left = Math.max(12, Math.min(left, vw - w - 12));
      top = Math.max(56, Math.min(top, vh - h - 8));
      el.style.left = left + "px";
      el.style.top = top + "px";
      el.style.setProperty("--ax", Math.max(12, Math.min(w - 24, r.left + Math.min(28, r.width / 2) - left - 6)) + "px");
    }
  }
  window.addEventListener("resize", tourPlace);
  msgs.addEventListener("scroll", tourPlace, { passive: true });
  setInterval(tourPlace, 300);
  if (MOBILE.addEventListener) MOBILE.addEventListener("change", tourPlace);

  function endTour() {
    tour.step = "free";
    Object.keys(tour.bubbles).forEach(hideBubble);
    $("skiptour").hidden = true;
    $("input").placeholder = "Message #bot-commands";
  }
  function tourDone(what) {
    if (what === "send" && tour.step === "send") { hideBubble("send"); tour.step = "link"; }
    else if (what === "web" || what === "cmd") {
      hideBubble(what);
      if (tour.step === "try" && !tour.bubbles.web && !tour.bubbles.cmd) endTour();
    }
  }
  function tourPanelPosted() {
    if (tour.step !== "link") return;
    bubble("link", "Click the <b>clip link</b> to open the bot's web page" + (MOBILE.matches ? "." : " on the right."),
      () => panelEl && panelEl.querySelector("a[data-clip]"), "top", "Step 2 of 3");
  }
  function tourFrameLoaded() {
    if (tour.step !== "link") return;
    hideBubble("link");
    tour.step = "try";
    bubble("web", "This is the clip page. Skip ⏭, click the <b>red bar</b> to seek, <b>drag</b> a track or click one to play it now. The Discord panel follows.",
      () => frame, "inside", "Step 3 of 3");
    bubble("cmd", "Or type a command here: <code>!pause</code>, <code>!next</code>, <code>!list</code>, <code>!help</code>",
      () => $("composer"), "top", MOBILE.matches ? "Step 3 of 3" : null);
    $("input").placeholder = `Try ${P}pause, ${P}next, ${P}list or ${P}help`;
  }
  $("skiptour").addEventListener("click", endTour);

  // ---- start
  renderVoice();
  post("maya", "can someone put on the friday playlist? 🎶", { time: "Today at " + new Date(Date.now() - 4 * 60000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) });
  post("theo", "you have the Spotify link, go ahead 👇", { time: "Today at " + new Date(Date.now() - 3 * 60000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) });
  lastAuthor = null;
  $("input").value = PLAY_CMD;
  bubble("send", (MOBILE.matches ? "Tap ➤ to send" : "Press <kbd>Enter</kbd> to send") + " this command. DJ Snake plays Spotify and YouTube links.",
    () => $("composer"), "top", "Step 1 of 3");
  if (!MOBILE.matches) $("input").focus({ preventScroll: true });
})();
