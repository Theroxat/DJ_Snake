"""Clip page themes: web routes, storage, logos, edit tokens and !theme."""

import json
import os

import pytest
from discord.ext import commands as ext_commands

from djsnake import commands, config, state, themes
from djsnake.player import MusicPlayer
from fakes import FakeAuthor, FakeChannel, make_ctx

GUILD_ID, OTHER_GUILD = 1, 2
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
GOOD = {
    "skin": "custom",
    "base": "club",
    "bg": "#112233",
    "text": "#EEEEEE",
    "accent": "#ff00aa",
    "name": "  Les Potes  ",
}
SAVED = {
    "skin": "custom",
    "base": "club",
    "bg": "#112233",
    "text": "#eeeeee",
    "accent": "#ff00aa",
    "name": "Les Potes",
    "logo_url": None,
}


@pytest.fixture
def token(player):
    return themes.issue_edit_token(GUILD_ID)


@pytest.fixture
def save(client, player):
    """POST a theme; returns (status, json)."""

    async def post(body, token, raw=None):
        headers = {"X-Edit-Token": token} if token is not None else {}
        url = f"/api/{player.sid}/theme"
        if raw is not None:
            r = await client.post(url, data=raw, headers=headers)
        else:
            r = await client.post(url, json=body, headers=headers)
        return r.status, await r.json()

    return post


@pytest.fixture
def logo(client, player):
    """POST (or DELETE) a logo; returns (status, json)."""

    async def send(data, token, method="POST"):
        r = await client.request(
            method,
            f"/api/{player.sid}/theme/logo",
            data=data,
            headers={"X-Edit-Token": token},
        )
        return r.status, await r.json()

    return send


async def get_theme(client, player):
    r = await client.get(f"/api/{player.sid}/state")
    return (await r.json())["theme"]


# --- Defaults ------------------------------------------------------------------


async def test_state_has_the_fallback_theme(client, player):
    assert await get_theme(client, player) == {
        "skin": "minimal",  # WEB_THEME, set by conftest
        "base": "night",
        "bg": "#101820",
        "text": "#f2f2f2",
        "accent": "#ffb703",
        "name": "",
        "logo_url": None,
    }


# --- Edit tokens -----------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [None, "x" * 32, "é" * 10, "other-guild"],
    ids=["missing", "wrong", "non-ascii", "other-guild"],
)
async def test_save_requires_this_guilds_token(save, player, given):
    if given == "other-guild":
        given = themes.issue_edit_token(OTHER_GUILD)
    status, body = await save(GOOD, given)
    assert status == 403 and body["ok"] is False
    assert "expired" in body["error"]


async def test_save_on_unknown_session_is_404(client, token):
    r = await client.post("/api/nope/theme", json=GOOD, headers={"X-Edit-Token": token})
    assert r.status == 404


async def test_expired_token_is_refused_and_pruned(save, token, fake_clock):
    fake_clock(31 * 60)
    status, _ = await save(GOOD, token)
    assert status == 403
    assert token not in state.edit_tokens


def test_token_is_valid_for_its_guild_only():
    token = themes.issue_edit_token(3)
    assert themes.check_edit_token(token, 3)
    assert not themes.check_edit_token(token, GUILD_ID)


# --- Validation and saving -----------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"bg": "red"},
        {"bg": "#112233\n"},
        {"accent": "#fff"},
        {"skin": "hacker"},
        {"base": "custom"},
        {"name": "x" * 33},
        {"name": "a\nb"},
        {"name": 5},
    ],
    ids=[
        "bad-color",
        "color-newline",
        "short-color",
        "bad-skin",
        "custom-as-base",
        "name-too-long",
        "name-control-char",
        "name-not-str",
    ],
)
async def test_invalid_theme_is_rejected(save, token, patch):
    status, body = await save({**GOOD, **patch}, token)
    assert status == 400 and body["ok"] is False and body["error"]
    assert GUILD_ID not in state.guild_themes


@pytest.mark.parametrize(
    "raw",
    [b"{not json", json.dumps({**GOOD, "pad": "x" * 5000})],
    ids=["bad-json", "oversize"],
)
async def test_bad_body_is_rejected(save, token, raw):
    status, _ = await save(None, token, raw=raw)
    assert status == 400
    assert GUILD_ID not in state.guild_themes


async def test_valid_theme_is_saved(client, save, player, token, data_dir):
    status, body = await save(GOOD, token)
    assert status == 200 and body["ok"] and body["theme"] == SAVED
    assert await get_theme(client, player) == SAVED
    with open(data_dir / "themes.json") as f:
        assert json.load(f)["1"]["skin"] == "custom"
    assert not [f for f in os.listdir(data_dir) if f.startswith(".tmp")]
    assert player.last_action and "theme" in player.last_action


async def test_failed_save_rolls_back_the_memory(save, token, monkeypatch):
    state.guild_themes[GUILD_ID] = {"skin": "club"}

    def unwritable():
        raise OSError("read-only")

    monkeypatch.setattr("djsnake.web.save_themes", unwritable)
    status, body = await save(GOOD, token)
    assert status == 500 and body["ok"] is False
    assert state.guild_themes[GUILD_ID] == {"skin": "club"}


# --- Logos ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "data, message",
    [
        (SVG, "Unsupported"),
        (b"<html>" + PNG, "Unsupported"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * (512 * 1024), "too large"),
    ],
    ids=["svg", "not-an-image", "oversize"],
)
async def test_bad_logo_is_rejected(logo, token, data, message):
    status, body = await logo(data, token)
    assert status == 400 and message in body["error"]


async def test_logo_requires_a_token(logo, token):
    status, _ = await logo(PNG, "bad-token-bad-token")
    assert status == 403


async def test_png_logo_is_stored_and_served(client, logo, player, token, data_dir):
    status, body = await logo(PNG, token)
    url = body["theme"]["logo_url"]
    assert status == 200 and url.startswith(f"/theme-logo/{player.sid}?v=")
    assert (data_dir / "logos" / "1.png").is_file()

    r = await client.get(url)
    assert r.status == 200 and await r.read() == PNG
    assert r.headers["Content-Type"] == "image/png"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert (await get_theme(client, player))["logo_url"] == url


async def test_new_logo_replaces_the_old_one(client, logo, token, data_dir):
    await logo(PNG, token)
    status, _ = await logo(GIF, token)
    assert status == 200
    assert (data_dir / "logos" / "1.gif").is_file()
    assert not (data_dir / "logos" / "1.png").exists()

    status, body = await logo(WEBP, token)
    r = await client.get(body["theme"]["logo_url"])
    assert r.status == 200 and r.headers["Content-Type"] == "image/webp"


async def test_logo_can_be_deleted(client, logo, player, token, data_dir):
    await logo(PNG, token)
    status, body = await logo(None, token, method="DELETE")
    assert status == 200 and body["theme"]["logo_url"] is None
    assert os.listdir(data_dir / "logos") == []
    r = await client.get(f"/theme-logo/{player.sid}")
    assert r.status == 404


async def test_logo_of_unknown_session_is_404(client):
    r = await client.get("/theme-logo/unknown")
    assert r.status == 404


# --- Storage -------------------------------------------------------------------


async def test_themes_reload_from_disk(save, logo, token):
    await save(GOOD, token)
    await logo(PNG, token)
    saved = dict(state.guild_themes[GUILD_ID])
    state.guild_themes.clear()
    themes.load_themes()
    assert state.guild_themes.get(GUILD_ID) == saved


def test_tampered_file_is_cleaned_on_load(data_dir):
    data_dir.mkdir()
    record = {**GOOD, "bg": "url(x)", "skin": "evil", "logo": {"ext": "svg", "v": 1}}
    with open(data_dir / "themes.json", "w") as f:
        json.dump({"1": record, "abc": {}}, f)
    themes.load_themes()
    assert state.guild_themes == {
        1: {
            "base": "club",
            "text": "#eeeeee",
            "accent": "#ff00aa",
            "name": "Les Potes",
        }
    }


def test_missing_file_loads_nothing():
    state.guild_themes[5] = {"skin": "club"}
    themes.load_themes()
    assert state.guild_themes == {}


# --- !theme --------------------------------------------------------------------


@pytest.fixture
def theme_ctx(channel):
    return make_ctx(GUILD_ID, channel, command=commands.theme)


async def test_theme_command_sets_a_preset(theme_ctx, channel):
    await commands.theme.callback(theme_ctx, "vinyl")
    assert themes.theme_for(GUILD_ID)["skin"] == "vinyl"
    assert channel.reactions == ["✅"]


async def test_theme_command_accepts_space(theme_ctx, channel):
    await commands.theme.callback(theme_ctx, "space")
    assert themes.theme_for(GUILD_ID)["skin"] == "space"
    assert channel.reactions == ["✅"]


async def test_space_is_saved_persisted_and_in_state(
    client, save, player, token, data_dir
):
    body = dict(GOOD, skin="space", base="space")
    status, res = await save(body, token)
    assert status == 200 and res["theme"]["skin"] == "space"
    assert res["theme"]["base"] == "space"
    assert (await get_theme(client, player))["skin"] == "space"
    with open(data_dir / "themes.json") as f:
        assert json.load(f)["1"]["skin"] == "space"


async def test_space_works_as_custom_base(client, save, player, token):
    status, res = await save(dict(GOOD, base="space"), token)
    assert status == 200 and res["theme"]["skin"] == "custom"
    assert (await get_theme(client, player))["base"] == "space"


async def test_theme_command_accepts_cobalt(theme_ctx, channel):
    await commands.theme.callback(theme_ctx, "cobalt")
    assert themes.theme_for(GUILD_ID)["skin"] == "cobalt"
    assert channel.reactions == ["✅"]


async def test_cobalt_is_saved_persisted_and_in_state(
    client, save, player, token, data_dir
):
    body = dict(GOOD, skin="cobalt", base="cobalt")
    status, res = await save(body, token)
    assert status == 200 and res["theme"]["skin"] == "cobalt"
    assert res["theme"]["base"] == "cobalt"
    assert (await get_theme(client, player))["skin"] == "cobalt"
    with open(data_dir / "themes.json") as f:
        assert json.load(f)["1"]["skin"] == "cobalt"


async def test_cobalt_works_as_custom_base(client, save, player, token):
    status, res = await save(dict(GOOD, base="cobalt"), token)
    assert status == 200 and res["theme"]["skin"] == "custom"
    assert (await get_theme(client, player))["base"] == "cobalt"


def test_cobalt_is_listed_everywhere_the_page_needs_it():
    with open(config.WEB_INDEX, encoding="utf-8") as f:
        page = f.read()
    assert '[data-skin="cobalt"]' in page
    assert '["cobalt", "Cobalt"]' in page
    assert page.count('<option value="cobalt">Cobalt</option>') == 2


async def test_theme_command_rejects_unknown_names(theme_ctx, channel):
    await commands.theme.callback(theme_ctx, "bogus")
    assert "Unknown theme" in channel.log[-1]


async def test_theme_command_shows_the_current_theme(theme_ctx, channel):
    await commands.theme.callback(theme_ctx, "vinyl")
    await commands.theme.callback(theme_ctx, None)
    assert "vinyl" in channel.log[-1] and "terminal" in channel.log[-1]
    assert "space" in channel.log[-1] and "cobalt" in channel.log[-1]


async def test_theme_reset_restores_the_fallback(theme_ctx):
    await commands.theme.callback(theme_ctx, "vinyl")
    await commands.theme.callback(theme_ctx, "reset")
    assert GUILD_ID not in state.guild_themes
    assert themes.theme_for(GUILD_ID)["skin"] == "minimal"


async def test_theme_edit_opens_a_session_and_dms_the_link(ffmpeg):
    channel, author = FakeChannel(), FakeAuthor()
    await commands.theme.callback(make_ctx(3, channel, author), "edit")
    player = state.music_players.get(3)
    assert player and player.sid and state.sessions.get(player.sid) == 3
    assert not player.in_voice()
    assert author.dms and f"/{player.sid}?edit=" in author.dms[0]
    assert not any("edit=" in message for message in channel.log)
    token = author.dms[0].rsplit("edit=", 1)[1].strip()
    assert themes.check_edit_token(token, 3)
    assert not themes.check_edit_token(token, GUILD_ID)


async def test_theme_edit_with_closed_dms_sends_no_link(ffmpeg):
    channel = FakeChannel()
    state.music_players[3] = MusicPlayer()
    await commands.theme.callback(make_ctx(3, channel, FakeAuthor(False)), "edit")
    assert "direct message" in channel.log[-1]
    assert "edit=" not in channel.log[-1]
    assert state.edit_tokens == {}


async def test_missing_permission_message(theme_ctx, channel):
    await commands.on_command_error(
        theme_ctx, ext_commands.MissingPermissions(["manage_guild"])
    )
    assert "Manage Server" in channel.log[-1]


def test_theme_command_needs_manage_guild():
    names = [check.__qualname__ for check in commands.theme.checks]
    assert any(n.startswith("has_guild_permissions") for n in names)


async def test_page_is_served_without_leaking_the_token(client, player):
    r = await client.get(f"/{player.sid}?edit=abc")
    assert r.status == 200
    assert r.headers.get("Referrer-Policy") == "same-origin"


def test_legacy_theme_names_are_migrated():
    record = themes._clean_theme({"skin": "perso", "base": "vinyle"})
    assert record == {"skin": "custom", "base": "vinyl"}
    assert themes._clean_theme({"skin": "nuit"}) == {"skin": "night"}
