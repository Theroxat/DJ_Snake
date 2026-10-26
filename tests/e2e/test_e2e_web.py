"""End-to-end clip page: the real aiohttp app on a free port, a session
playing generated media through the real playback chain (FFmpeg frames read
at real time), and web/index.html driven in headless Chromium.

Run with `pytest -m e2e`; skipped without ffmpeg, Playwright or its Chromium
(`python -m playwright install chromium`)."""

import json
import re
import socket
import time
import types
import urllib.parse

import pytest
from aiohttp import web as aioweb

from djsnake import lyrics, media, state
from djsnake.playback import play_next
from djsnake.player import notify_state
from djsnake.web import build_web_app
from e2e_kit import needs_ffmpeg, wait_until

playwright_api = pytest.importorskip("playwright.async_api")
expect = playwright_api.expect

pytestmark = [pytest.mark.e2e, needs_ffmpeg]

SYNC_TOLERANCE = 0.3  # Seconds between the video and the server clock


@pytest.fixture
async def session(media_kit, live_player, e2e_ctx, media_dir):
    """A player in voice playing the chirp, the tone queued after it."""
    player = await live_player(1.0)
    chirp = media_kit.track("chirp", media_dir)
    tone = media_kit.track("tone", media_dir)
    player.add_to_queue(chirp)
    player.add_to_queue(tone)
    await play_next(e2e_ctx)
    assert player.now_playing is chirp
    return types.SimpleNamespace(player=player, chirp=chirp, tone=tone)


@pytest.fixture
async def server():
    """The real web app (build_web_app) on a free local port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    runner = aioweb.AppRunner(build_web_app(), access_log=None, shutdown_timeout=1)
    await runner.setup()
    await aioweb.SockSite(runner, sock).start()
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    await runner.cleanup()  # Ends the open live streams first


@pytest.fixture
async def browser():
    async with playwright_api.async_playwright() as p:
        try:
            chromium = await p.chromium.launch()
        except playwright_api.Error as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        yield chromium
        await chromium.close()


async def open_page(browser, url, **context_options):
    """A page in a fresh context (empty localStorage); page errors are
    collected in page.errors."""
    context = await browser.new_context(**context_options)
    page = await context.new_page()
    page.set_default_timeout(10_000)
    page.errors = []
    page.on("pageerror", lambda error: page.errors.append(error))
    await page.goto(url)
    return page


async def wait_playing(page, title):
    await expect(page.locator("#title")).to_contain_text(title)
    await page.wait_for_function("""() => { const v = document.getElementById("video");
                   return v.readyState >= 2 && !v.paused && v.currentTime > 0; }""")


async def drift(page, player):
    """How far the video is outside the server's elapsed time, in seconds."""
    before = player.elapsed()
    position = await page.evaluate("document.getElementById('video').currentTime")
    after = player.elapsed()
    return max(before - position, position - after, 0.0)


async def assert_in_sync(page, player, samples=5, timeout=10.0):
    """The video settles within SYNC_TOLERANCE and stays there."""
    deadline = time.monotonic() + timeout
    good = 0
    while good < samples:
        if time.monotonic() > deadline:
            raise AssertionError(f"Video out of sync by {await drift(page, player)} s")
        good = good + 1 if await drift(page, player) <= SYNC_TOLERANCE else 0
        await page.wait_for_timeout(100)  # Let the video play between samples


async def test_page_loads_and_the_video_follows_the_server_clock(
    session, server, browser
):
    player = session.player
    page = await open_page(browser, f"{server}/{player.sid}")
    await wait_playing(page, "Chirp")
    await assert_in_sync(page, player)
    await expect(page.locator("#live")).not_to_have_class(re.compile(r"\boff\b"))
    await expect(page.locator("#queue")).to_contain_text("Tone")
    assert page.errors == []


async def test_live_stream_pushes_a_server_side_queue_change(
    session, server, browser, media_kit, media_dir
):
    player = session.player
    page = await open_page(browser, f"{server}/{player.sid}")
    await expect(page.locator("#title")).to_contain_text("Chirp")
    await wait_until(lambda: state.streams.get(player.sid), what="the live stream")
    # From now on, a change can only reach the page through the stream.
    await page.route(re.compile(r"/api/[^/]+/state"), lambda route: route.abort())

    pushed = media_kit.track("tone", media_dir, title="Pushed Track")
    player.add_to_queue(pushed)
    notify_state(player)
    await expect(page.locator("#queue")).to_contain_text("Pushed Track")
    assert page.errors == []


async def test_next_button_skips_on_the_server(session, server, browser):
    player = session.player
    page = await open_page(browser, f"{server}/{player.sid}")
    await wait_playing(page, "Chirp")
    await page.click("#next")
    await wait_until(lambda: player.now_playing is session.tone, what="the skip")
    await expect(page.locator("#title")).to_contain_text("Tone")
    await expect(page.locator("#video")).to_have_attribute(
        "src", re.compile(session.tone["id"])
    )
    await wait_playing(page, "Tone")
    assert page.errors == []


async def test_pause_and_resume_reach_the_server(session, server, browser):
    player = session.player
    page = await open_page(browser, f"{server}/{player.sid}")
    await wait_playing(page, "Chirp")

    await page.click("#pp")
    await wait_until(
        lambda: player.paused_at is not None and player.voice_client.is_paused(),
        what="the pause",
    )
    await expect(page.locator("#pausebadge")).to_be_visible()
    await page.wait_for_function("() => document.getElementById('video').paused")

    await expect(page.locator("#pp")).to_be_enabled()
    await page.click("#pp")
    await wait_until(
        lambda: player.paused_at is None and player.voice_client.is_playing(),
        what="the resume",
    )
    await expect(page.locator("#pausebadge")).to_be_hidden()
    await wait_playing(page, "Chirp")
    assert page.errors == []


async def test_seek_bar_seeks_on_the_server(session, server, browser):
    player = session.player
    page = await open_page(browser, f"{server}/{player.sid}")
    await wait_playing(page, "Chirp")
    gen, before = player.play_gen, player.elapsed()

    await page.focus("#seek")
    await page.keyboard.press("ArrowRight")  # +5 s
    await wait_until(
        lambda: player.play_gen > gen and player.pending_action is None,
        what="the seek",
    )
    assert player.now_playing is session.chirp
    assert player.elapsed() == pytest.approx(before + 5, abs=1.5)
    await assert_in_sync(page, player)
    assert page.errors == []


async def test_add_form_queues_a_link(
    session, server, browser, media_kit, media_dir, monkeypatch
):
    player = session.player
    asked = []

    def fake_download(url, on_title=None, **kwargs):
        asked.append(url)
        return media_kit.track("tone", media_dir, title="Added Track")

    monkeypatch.setattr(media, "download_media", fake_download)
    page = await open_page(browser, f"{server}/{player.sid}")
    await expect(page.locator("#title")).to_contain_text("Chirp")

    await page.fill("#addurl", "https://youtu.be/dQw4w9WgXcQ")
    await page.click("#addbtn")
    await wait_until(
        lambda: any(t["title"] == "Added Track" for t in player.queue),
        what="the added track",
    )
    assert asked == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]  # Canonical
    await expect(page.locator("#queue")).to_contain_text("Added Track")
    await expect(page.locator("#addurl")).to_have_value("")
    assert page.errors == []


async def test_view_only_link_hides_controls_and_is_refused(session, server, browser):
    player = session.player
    page = await open_page(browser, f"{server}/{player.watch_id}")
    await wait_playing(page, "Chirp")
    await expect(page.locator("html")).to_have_attribute("data-viewonly", "")
    await expect(page.locator("#viewonly")).to_be_visible()
    for hidden in ("#next", "#pp", "#prev", "#addform"):
        await expect(page.locator(hidden)).to_be_hidden()
    assert player.sid not in page.url
    video_src = await page.locator("#video").get_attribute("src")
    assert player.watch_id in video_src and player.sid not in video_src

    status = await page.evaluate(
        """async (link) => (await fetch("/api/" + link + "/control", {
               method: "POST", headers: {"Content-Type": "application/json"},
               body: JSON.stringify({action: "next"})})).status""",
        player.watch_id,
    )
    assert status == 403
    assert player.now_playing is session.chirp and player.pending_action is None
    assert page.errors == []


async def test_theme_menu_switches_and_remembers_the_preset(session, server, browser):
    player = session.player
    page = await open_page(browser, f"{server}/{player.sid}")
    html = page.locator("html")
    await expect(page.locator("#title")).to_contain_text("Chirp")
    await expect(html).to_have_attribute("data-skin", "minimal")  # WEB_THEME
    background = "getComputedStyle(document.body).backgroundColor"
    minimal_bg = await page.evaluate(background)

    await page.click("#palbtn")
    await page.click('#thememenu [data-choice="night"]')
    await expect(html).to_have_attribute("data-skin", "night")
    assert await page.evaluate(background) != minimal_bg
    stored = await page.evaluate("localStorage.getItem('djsnake.skin')")
    assert stored == "night"

    await page.reload()
    await expect(page.locator("#title")).to_contain_text("Chirp")
    await expect(html).to_have_attribute("data-skin", "night")
    assert page.errors == []


class FakeLrclib:
    """lyrics._http stand-in: one synced line per second of file time."""

    def __init__(self):
        self.calls = []

    def __call__(self, url):
        parsed = urllib.parse.urlsplit(url)
        self.calls.append(parsed.path)
        lines = "\n".join(f"[00:{s:02d}.00] Line {s}" for s in range(25))
        record = {
            "trackName": "Chirp",
            "artistName": "E2E Artist",
            "duration": 21.5,
            "instrumental": False,
            "plainLyrics": "\n".join(f"Line {s}" for s in range(25)),
            "syncedLyrics": lines,
        }
        body = record if parsed.path.endswith("/get") else [record]
        return 200, json.dumps(body).encode()


async def test_lyrics_panel_follows_the_playback(session, server, browser, monkeypatch):
    player = session.player
    lrclib = FakeLrclib()
    monkeypatch.setattr(lyrics, "_http", lrclib)
    page = await open_page(browser, f"{server}/{player.sid}")
    await wait_playing(page, "Chirp")
    await expect(page.locator("#lyrbtn")).to_be_visible()

    await page.click("#lyrbtn")
    await expect(page.locator("#lyrics")).to_be_visible()
    current = page.locator("#lyrlines li.cur")
    await expect(current).to_have_count(1)
    shown = int((await current.inner_text()).split()[-1])
    assert abs(shown - int(player.elapsed())) <= 1  # Line N is sung at N s
    await expect(current).to_have_text(f"Line {shown + 1}")  # It moves on
    assert lrclib.calls and lrclib.calls[0].endswith("/get")
    assert page.errors == []


async def test_narrow_screen_has_no_horizontal_scroll(session, server, browser):
    player = session.player
    page = await open_page(
        browser, f"{server}/{player.sid}", viewport={"width": 320, "height": 640}
    )
    await wait_playing(page, "Chirp")
    await expect(page.locator("#queue")).to_contain_text("Tone")
    overflow = await page.evaluate(
        "document.documentElement.scrollWidth - window.innerWidth"
    )
    assert overflow <= 0
    assert page.errors == []
