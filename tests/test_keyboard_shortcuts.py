"""Keyboard shortcuts of the clip page.

String-level checks always run. The browser tests drive the static demo
(docs/demo/player.html) in headless Chromium and are skipped when Playwright or
its browser is not installed (no network is used: the demo fakes the bot).
"""

import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
DEMO = ROOT / "docs" / "demo"


def test_page_has_help_dialog_and_button():
    assert '<dialog id="kbdlg"' in PAGE
    assert 'id="kbbtn"' in PAGE
    assert 'aria-haspopup="dialog"' in PAGE.split('id="kbbtn"')[1][:200]


def test_shortcut_map_documents_every_binding():
    block = PAGE.split("const SHORTCUTS = [", 1)[1].split("];", 1)[0]
    for label in (
        "kb_playpause kb_seekb kb_seekf kb_next kb_prev kb_shuffle kb_loop "
        "kb_mute kb_volup kb_voldn kb_fs kb_lyr kb_theme kb_help kb_esc"
    ).split():
        assert label in block


def test_help_strings_in_both_languages():
    en, fr = PAGE.split("    fr: {", 1)
    keys = {
        k
        for k in set(__import__("re").findall(r'"(kb_[a-z_]+)"', PAGE))
        if not k.endswith("_")
    }
    assert keys
    for key in keys:
        assert f"      {key}:" in en, key
        assert f"      {key}:" in fr, key


def test_listener_ignores_fields_and_modifiers():
    code = PAGE.split(
        'document.addEventListener("keydown", (e) => {\n    if (e.defaultPrevented || e.isComposing',
        1,
    )[1]
    head = code[:700]
    assert "e.ctrlKey || e.metaKey || e.altKey" in head
    assert "input, textarea, select" in head
    assert "contenteditable" in head
    assert "active()" in code  # control keys need a control link in voice


def test_layout_independent_matching():
    assert "function kbLetter" in PAGE
    assert 'e.key === "?"' in PAGE


def test_docs_list_shortcuts():
    for doc in (ROOT / "README.md", ROOT / "docs" / "source" / "clip-page.md"):
        text = doc.read_text(encoding="utf-8")
        assert "Keyboard shortcuts" in text, doc


# ---- headless Chromium on the demo


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser binary missing
            pytest.skip(f"no Chromium: {e}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def base():
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(DEMO)
    )
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/player.html"
    srv.shutdown()


HOOK = """
window.__posts = [];
const f = window.fetch;
window.fetch = function (url, opt) {
  if (opt && opt.method === "POST" && String(url).includes("/control")) {
    try { window.__posts.push(JSON.parse(opt.body)); } catch (e) {}
  }
  return f.apply(this, arguments);
};
0;
"""


def hook(page):
    page.evaluate(HOOK)  # the demo shim replaces fetch: record what it receives


def posts_of(page):
    return page.evaluate("window.__posts")


def open_page(browser, url, width=1100):
    page = browser.new_page(viewport={"width": width, "height": 800})
    page.goto(url)
    page.wait_for_selector("#pp:not([disabled])", timeout=15000)
    hook(page)
    return page


def actions(page):
    return [p["action"] for p in posts_of(page)]


def test_browser_play_pause_seek(browser, base):
    page = open_page(browser, base)
    page.keyboard.press("Space")
    page.wait_for_timeout(300)
    assert actions(page)[:1] in (["pause"], ["resume"])
    page.keyboard.press("k")
    page.wait_for_timeout(300)
    acts = actions(page)
    assert len(acts) == 2 and acts[0] != acts[1]
    page.keyboard.press("l")
    page.wait_for_timeout(300)
    assert actions(page)[-1] == "seek"
    page.close()


def test_browser_help_opens_closes_and_returns_focus(browser, base):
    page = open_page(browser, base)
    page.keyboard.press("Shift+?")
    assert page.is_visible("#kbdlg")
    assert page.evaluate("document.activeElement.id") == "kbclose"
    assert page.locator("#kbrows kbd").count() > 10
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")  # trapped: still inside the dialog
    assert page.evaluate("!!document.activeElement.closest('#kbdlg')")
    page.keyboard.press("Escape")
    assert not page.is_visible("#kbdlg")
    page.focus("#kbbtn")
    page.keyboard.press("Enter")
    assert page.is_visible("#kbdlg")
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.id") == "kbbtn"
    page.close()


def test_browser_ignores_typing_and_modifiers(browser, base):
    page = open_page(browser, base)
    page.focus("#addurl")
    page.keyboard.type("k?n")
    assert not page.is_visible("#kbdlg")
    assert page.input_value("#addurl") == "k?n"
    page.locator("#addurl").evaluate("e => e.blur()")
    page.keyboard.press("Control+k")
    page.keyboard.press("Alt+n")
    page.wait_for_timeout(200)
    assert posts_of(page) == []
    page.close()


def test_browser_view_only_skips_control_keys(browser, base):
    page = open_page_view(browser, base + "?view")
    for key in ("Space", "k", "n", "p", "s", "r", "m", "ArrowUp", "ArrowRight"):
        page.keyboard.press(key)
    page.wait_for_timeout(300)
    assert posts_of(page) == []
    page.keyboard.press("?")
    assert page.is_visible("#kbdlg")
    assert "view-only" in page.inner_text("#kbnote")
    page.close()


def open_page_view(browser, url):
    page = browser.new_page(viewport={"width": 1100, "height": 800})
    page.goto(url)
    page.wait_for_selector("html[data-viewonly]", timeout=15000)
    hook(page)
    return page


def test_browser_help_fits_320px(browser, base):
    page = browser.new_page(viewport={"width": 320, "height": 640})
    page.goto(base)
    page.wait_for_selector("#kbbtn")
    page.keyboard.press("?")
    assert page.is_visible("#kbdlg")
    assert page.evaluate("document.documentElement.scrollWidth") <= 320
    assert page.evaluate("document.getElementById('kbdlg').scrollWidth") <= 320
    page.close()
