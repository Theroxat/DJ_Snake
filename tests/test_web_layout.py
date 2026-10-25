"""Headless Chromium layout checks for the clip page (skipped without playwright)."""

import functools
import http.server
import pathlib
import threading

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

DEMO = pathlib.Path(__file__).resolve().parent.parent / "docs" / "demo"
SKINS = ["arcade", "minimal", "night", "vinyl", "club", "terminal", "space", "cobalt"]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def browser():
    handler = functools.partial(_Quiet, directory=str(DEMO))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with sync_api.sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception as exc:  # browser binary not installed
            server.shutdown()
            pytest.skip(f"chromium unavailable: {exc}")
        yield br, f"http://127.0.0.1:{server.server_address[1]}/player.html"
        br.close()
    server.shutdown()


JS = """() => {
  const deck = document.querySelector('.transport').parentElement.getBoundingClientRect();
  return {
    deck: [deck.left, deck.right],
    scroll: document.documentElement.scrollWidth,
    buttons: [...document.querySelectorAll('.tcenter button')].map(b => {
      const r = b.getBoundingClientRect();
      return {id: b.id, left: r.left, right: r.right, width: r.width, height: r.height};
    }),
  };
}"""


@pytest.mark.parametrize("width", [320, 360])
@pytest.mark.parametrize("skin", SKINS)
def test_transport_buttons_stay_inside_player(browser, skin, width):
    br, url = browser
    ctx = br.new_context(viewport={"width": width, "height": 700})
    ctx.add_init_script(
        f"try{{localStorage.setItem('djsnake.skin','{skin}')}}catch(e){{}}"
    )
    page = ctx.new_page()
    try:
        page.goto(url)
        page.wait_for_selector(".tcenter #loop")
        page.wait_for_timeout(500)
        got = page.evaluate(JS)
    finally:
        ctx.close()
    ids = [b["id"] for b in got["buttons"]]
    assert ids == ["shuffle", "prev", "pp", "next", "loop"]
    for b in got["buttons"]:
        assert b["left"] >= got["deck"][0] - 0.5, (skin, b)
        assert b["right"] <= got["deck"][1] + 0.5, (skin, b)
        assert b["width"] >= 40 and b["height"] >= 40, (skin, b)
    assert got["scroll"] <= width, skin
