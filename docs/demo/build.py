#!/usr/bin/env python3
"""Regenerate docs/demo/player.html from web/index.html plus the demo shim.

player.html is the real clip page running on the fake bot in backend.js. The
demo's entry page, index.html (a mock Discord client, hand-written), loads it
in an iframe; opened on its own it plays a standalone session.

Run from anywhere: python3 docs/demo/build.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "web" / "index.html"
OUT = ROOT / "docs" / "demo" / "player.html"

RIBBON_CSS = """
<style>
html:not(.demo-embedded) body { padding-bottom: 56px; }
html.demo-embedded .demo-ribbon { display: none; }
.demo-ribbon { position: fixed; z-index: 99999; left: 50%; bottom: 10px; transform: translateX(-50%);
  display: flex; gap: 10px; align-items: center; padding: 6px 14px; border-radius: 999px;
  background: rgba(16,16,16,.88); color: #fff; font: 600 12px/1.2 system-ui, sans-serif;
  letter-spacing: .02em; box-shadow: 0 2px 12px rgba(0,0,0,.4); max-width: calc(100vw - 24px); }
.demo-ribbon a { color: #ffd166; text-decoration: underline; white-space: nowrap; }
.demo-ribbon a:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
@media (max-width: 520px) { .demo-ribbon { font-size: 11px; gap: 8px; padding: 6px 10px; } }
</style>
"""

RIBBON_HTML = """
<div class="demo-ribbon" role="note">
  <span title="Excerpts of Caminandes 3, Cosmos Laundromat, Big Buck Bunny, Tears of Steel and Sintel (Blender Foundation / Blender Studio, CC BY licence).">Demo: CC BY film excerpts (Blender)</span>
  <a href="./">Discord demo</a>
  <a href="?edit=demo">Theme editor</a>
  <a href="../#credits">Credits</a>
</div>
"""


def main():
    html = SRC.read_text(encoding="utf-8")
    # The shim must run before the page's own scripts: put it first in <head>.
    head = '<script src="backend.js"></script>\n<script src="shim.js"></script>\n'
    assert "<head>" in html and "</body>" in html
    html = html.replace("<head>", "<head>\n" + head, 1)
    html = html.replace("</head>", RIBBON_CSS + "</head>", 1)
    html = html.replace("</body>", RIBBON_HTML + "</body>", 1)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
