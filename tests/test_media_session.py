"""Clip page Media Session wiring (string-level checks on web/index.html, plus a
headless Chromium run of the static demo when playwright is installed)."""

import functools
import http.server
import os
import re
import threading

import pytest

from djsnake import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "docs", "demo")


@pytest.fixture(scope="module")
def page():
    with open(config.WEB_INDEX, encoding="utf-8") as f:
        return f.read()


def test_handlers_are_wired_to_the_control_api(page):
    for action, call in [
        ("play", 'send("resume")'),
        ("pause", 'send("pause")'),
        ("nexttrack", 'send("next")'),
        ("previoustrack", 'send("previous")'),
        ("seekto", "seekTo(state.start + d.seekTime)"),
        ("seekbackward", "curElapsed() - "),
        ("seekforward", "curElapsed() + "),
    ]:
        m = re.search(r'msSet\("%s", on \? (.+)' % action, page)
        assert m, action
        assert call in m.group(1), action
    assert "const MS_SEEK_STEP = 10;" in page


def test_view_only_registers_no_handlers(page):
    # Handlers are bound from the control flag, and every handler is gated.
    assert "msBind(canControl);" in page
    assert "function msBind(on)" in page
    assert page.count("on ? ") >= 7  # each action is null when `on` is false
    assert "if (!MS || on === msHandlers) return;" in page


def test_metadata_position_and_cleanup(page):
    assert "new MediaMetadata(meta)" in page
    assert "MS.playbackState = paused" in page
    assert "MS.setPositionState" in page
    assert "playbackRate: 1" in page
    for size in ("96x96", "512x512"):
        assert size in page
    # Session end and idle/out-of-voice states clear everything.
    assert re.search(r"function showEnded\(\) \{.*?msClear\(\);", page, re.S)
    assert page.count("msClear();") >= 3
    assert "MS.metadata = null" in page
    assert "msBind(false);" in page


def test_silent_audio_starts_on_user_gesture(page):
    assert '"pointerdown", "keydown", "touchend"' in page
    assert "msAudio.loop = true" in page
    assert 'type: "audio/wav"' in page


@pytest.fixture
def demo_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=DEMO)
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_demo_sets_media_session_metadata(demo_server):
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary not installed
            pytest.skip(str(e))
        pg = browser.new_page()
        pg.add_init_script(
            "window.__h = {}; const o = MediaSession.prototype.setActionHandler;"
            "MediaSession.prototype.setActionHandler = function (a, f) {"
            " window.__h[a] = !!f; return o.call(this, a, f); };"
        )
        pg.goto("http://127.0.0.1:%d/player.html" % demo_server)
        pg.wait_for_function(
            "navigator.mediaSession.metadata && navigator.mediaSession.metadata.title",
            timeout=15000,
        )
        meta = pg.evaluate(
            "({t: navigator.mediaSession.metadata.title,"
            " al: navigator.mediaSession.metadata.album,"
            " art: navigator.mediaSession.metadata.artwork.length,"
            " st: navigator.mediaSession.playbackState, h: window.__h})"
        )
        browser.close()
    assert meta["t"]
    assert meta["art"] >= 1
    assert meta["st"] in ("playing", "paused")
    assert meta["h"].get("nexttrack") is True
    assert meta["h"].get("seekto") is True
