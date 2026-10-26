"""PWA: manifest per session, service worker route and rules, icons."""

import json
import re
from pathlib import Path

from djsnake import config, state, themes

GUILD_ID = 1
SW_SOURCE = Path(config.WEB_SW).read_text(encoding="utf-8")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


async def manifest(client, link):
    r = await client.get(f"/{link}/manifest.webmanifest")
    return r, (await r.json() if r.status == 200 else None)


async def test_manifest_of_the_control_link(client, player):
    r, m = await manifest(client, player.sid)
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("application/manifest+json")
    assert r.headers["Cache-Control"] == "no-cache"
    assert m["start_url"] == m["id"] == f"/{player.sid}"
    assert m["display"] == "standalone" and m["scope"] == "/"
    assert m["name"] == "DJ Snake" and m["lang"] == "en"
    assert m["description"]
    assert (
        m["theme_color"] == m["background_color"] == themes.PRESET_BG[config.WEB_THEME]
    )
    sizes = {(i["sizes"], i.get("purpose", "any")) for i in m["icons"]}
    assert sizes == {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")}
    for icon in m["icons"]:
        assert icon["type"] == "image/png"
        assert (await client.get(icon["src"])).status == 200


async def test_view_only_manifest_never_names_the_control_id(client, player):
    r, m = await manifest(client, player.watch_id)
    assert r.status == 200
    assert m["start_url"] == m["id"] == f"/{player.watch_id}"
    assert player.sid not in json.dumps(m)


async def test_unknown_session_has_no_manifest(client, player):
    r, _ = await manifest(client, "nope-nope-nope-nope")
    assert r.status == 404


async def test_manifest_follows_the_guild_theme(client, player):
    state.guild_themes[GUILD_ID] = {
        "skin": "custom",
        "base": "club",
        "bg": "#112233",
        "text": "#eeeeee",
        "accent": "#ff00aa",
        "name": "Les Potes Du Vendredi",
    }
    _, m = await manifest(client, player.sid)
    assert m["name"] == "Les Potes Du Vendredi" and m["short_name"] == "Les Potes Du"
    assert m["theme_color"] == m["background_color"] == "#112233"


async def test_manifest_uses_a_preset_background_and_language(client, player):
    state.guild_themes[GUILD_ID] = {"skin": "cobalt"}
    state.guild_languages[GUILD_ID] = "fr"
    _, m = await manifest(client, player.sid)
    assert m["background_color"] == themes.PRESET_BG["cobalt"] and m["lang"] == "fr"
    assert "télécommande" in m["description"]


async def test_manifest_uses_the_logo_with_the_requesting_id(client, player):
    themes.set_logo(GUILD_ID, PNG, "png")
    _, control = await manifest(client, player.sid)
    _, view = await manifest(client, player.watch_id)
    for link, m in ((player.sid, control), (player.watch_id, view)):
        srcs = {i["src"] for i in m["icons"]}
        assert len(srcs) == 1 and srcs.pop().startswith(f"/theme-logo/{link}?v=")
        assert all(i["type"] == "image/png" for i in m["icons"])
    assert player.sid not in json.dumps(view)


async def test_service_worker_route(client):
    r = await client.get("/sw.js")
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/javascript")
    assert r.headers["Service-Worker-Allowed"] == "/"
    assert r.headers["Cache-Control"] == "no-cache"
    body = await r.text()
    version = re.sub(r"\W", "_", config.APP_VERSION)
    assert "__VERSION__" not in body
    assert f'const VERSION = "{version}"' in body


def test_service_worker_never_caches_the_api():
    # Only the page (/{sid}) and the font hosts are intercepted...
    assert "isPage(url)" in SW_SOURCE and "FONT_HOSTS" in SW_SOURCE
    assert re.search(r'request\.method !== "GET"\) return', SW_SOURCE)
    # ...so the API, the live stream, media, lyrics and logos are never named
    # in a respondWith or cache call (code only, comments aside).
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", SW_SOURCE, flags=re.S)
    for forbidden in ("/api", "/media", "events", "lyrics", "theme-logo"):
        assert forbidden not in code
    # The one stored page has a fixed key, never a URL with a session id.
    assert 'SHELL_KEY = "/__shell"' in SW_SOURCE


async def test_icons_are_a_fixed_list(client):
    for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png"):
        r = await client.get(f"/icons/{name}")
        assert r.status == 200 and r.headers["Content-Type"] == "image/png"
        assert (await r.read()).startswith(b"\x89PNG")
    assert (await client.get("/icons/sw.js")).status == 404
    assert (await client.get("/icons/..%2Fweb.py")).status == 404


async def test_page_links_the_manifest_and_icons(client, player):
    r = await client.get(f"/{player.sid}")
    text = await r.text()
    assert r.status == 200 and "manifest.webmanifest" in text
    assert "/icons/apple-touch-icon.png" in text
