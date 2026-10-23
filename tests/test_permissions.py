"""Who may control the music: view-only links, the DJ role and !weblink."""

import asyncio
import json
import types

import pytest
from discord.ext import commands as ext_commands

from djsnake import commands, permissions, sessions, state, web
from djsnake.playback import play_next
from djsnake.player import MusicPlayer
from djsnake.sessions import render_panel, resolve_link
from fakes import FakeAuthor, FakeChannel, make_track, settle

GUILD_ID = 1
DJ_ROLE = 555


def member(member_id=10, roles=(), manager=False, dms_open=True):
    """A guild member (FakeAuthor, so DMs work) with roles and permissions."""
    m = FakeAuthor(dms_open)
    m.id = member_id
    m.display_name = f"member{member_id}"
    m.roles = [types.SimpleNamespace(id=r) for r in roles]
    m.guild_permissions = types.SimpleNamespace(manage_guild=manager)
    return m


def guild_ctx(channel, author, command=None, guild_id=GUILD_ID):
    role = types.SimpleNamespace(id=DJ_ROLE, name="DJ")
    guild = types.SimpleNamespace(
        id=guild_id,
        name="Guild",
        get_role=lambda rid: role if rid == DJ_ROLE else None,
    )
    return types.SimpleNamespace(
        guild=guild,
        channel=channel,
        author=author,
        send=channel.send,
        message=None,
        command=types.SimpleNamespace(name=command) if command else None,
    )


@pytest.fixture
async def playing(player, ctx, media_dir):
    """A playing (queued by member 10), B queued."""
    a = make_track(media_dir, "A", requester_id=10)
    b = make_track(media_dir, "B")
    player.add_to_queue(a)
    player.add_to_queue(b)
    await play_next(ctx)
    return a, b


# --- Link mapping -----------------------------------------------------------


def test_session_has_a_distinct_watch_id(player):
    assert player.watch_id and player.watch_id != player.sid
    assert state.watch_ids[player.watch_id] == player.sid
    assert resolve_link(player.sid) == (player.sid, True)
    assert resolve_link(player.watch_id) == (player.sid, False)


@pytest.mark.parametrize("given", [None, "", "nope", "x" * 65, 42])
def test_unknown_ids_resolve_to_nothing(player, given):
    assert resolve_link(given) == (None, False)


def test_ids_are_compared_in_constant_time(player, monkeypatch):
    calls = []
    real = sessions.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(sessions.secrets, "compare_digest", spy)
    assert resolve_link(player.watch_id) == (player.sid, False)
    # Every live id is compared, whichever matches.
    assert {a for a, _ in calls} == {player.sid.encode(), player.watch_id.encode()}


def test_expiry_drops_the_watch_id(player):
    watch = player.watch_id
    player.voice_client = None
    sessions.expire_session(player)
    assert watch not in state.watch_ids
    assert resolve_link(watch) == (None, False)


async def test_ids_are_never_logged(ctx, capsys):
    p = MusicPlayer()
    state.music_players[GUILD_ID] = p
    await sessions.open_session(ctx, p, announce=False)
    ids = (p.sid, p.watch_id)
    sessions.expire_session(p)
    out = capsys.readouterr().out
    assert "opened" in out and "expired" in out
    assert all(i not in out for i in ids)


# --- Web API with a view-only id --------------------------------------------


async def test_watch_link_serves_page_and_state(client, player, playing):
    r = await client.get(f"/{player.watch_id}")
    assert r.status == 200
    r = await client.get(f"/api/{player.watch_id}/state")
    data = await r.json()
    assert r.status == 200 and data["can_control"] is False
    assert data["now_playing"]["title"] == "A"
    # Nothing in the view-only state leads to the control id.
    assert player.sid not in json.dumps(data)
    assert data["now_playing"]["video_url"].startswith(f"/media/{player.watch_id}/")
    r = await client.get(data["now_playing"]["video_url"])
    assert r.status == 200


async def _first_event(response):
    while True:
        line = await asyncio.wait_for(response.content.readline(), 2.0)
        assert line, "stream ended"
        if line.startswith(b"data: "):
            return line[len(b"data: ") :].decode()


async def test_watch_link_events_never_carry_the_control_id(client, player, playing):
    r = await client.get(f"/api/{player.watch_id}/events")
    assert r.status == 200
    raw = await _first_event(r)
    data = json.loads(raw)
    assert data["can_control"] is False and data["now_playing"]["title"] == "A"
    assert player.sid not in raw
    assert data["now_playing"]["video_url"].startswith(f"/media/{player.watch_id}/")
    r.close()


async def test_unknown_id_events_is_404(client, player):
    r = await client.get("/api/" + "x" * 22 + "/events")
    assert r.status == 404


async def test_rotation_ends_control_streams_keeps_watch_streams(
    client, player, playing, monkeypatch
):
    monkeypatch.setattr(web, "SSE_MIN_INTERVAL", 0.01)
    control = await client.get(f"/api/{player.sid}/events")
    watch = await client.get(f"/api/{player.watch_id}/events")
    await _first_event(control)
    await _first_event(watch)
    old = player.sid
    sessions.rotate_control_link(player)
    assert player.sid != old
    assert json.loads(await _first_event(control)) == {"active": False}
    raw = await _first_event(watch)
    assert json.loads(raw)["active"] is True and player.sid not in raw
    control.close()
    watch.close()


async def test_control_link_state_can_control(client, player):
    r = await client.get(f"/api/{player.sid}/state")
    assert (await r.json())["can_control"] is True


@pytest.mark.parametrize("action", sorted(web.CONTROL_ACTIONS) + ["bogus"])
async def test_control_with_watch_id_is_403(client, player, playing, action):
    r = await client.post(
        f"/api/{player.watch_id}/control",
        json={"action": action, "enabled": True, "position": 1, "url": "x"},
    )
    assert r.status == 403
    body = await r.json()
    assert body["ok"] is False and "view-only" in body["error"]
    assert player.now_playing["title"] == "A" and player.paused_at is None
    assert player.crossfade is False


async def test_theme_routes_refuse_watch_id(client, player):
    r = await client.post(
        f"/api/{player.watch_id}/theme",
        json={},
        headers={"X-Edit-Token": "x" * 32},
    )
    assert r.status == 403


async def test_control_with_control_id_still_works(client, player, playing):
    r = await client.post(f"/api/{player.sid}/control", json={"action": "pause"})
    assert r.status == 200
    assert player.paused_at is not None


# --- Status panel -----------------------------------------------------------


def test_panel_default_posts_both_links(player):
    text = render_panel(player)
    assert f"/{player.sid}" in text and f"/{player.watch_id}" in text


def test_panel_with_dj_role_hides_control_link(player):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    text = render_panel(player)
    assert player.sid not in text
    assert f"/{player.watch_id}" in text and "!weblink" in text


def test_panel_with_web_control_dj_hides_control_link(player, monkeypatch):
    monkeypatch.setattr(permissions, "WEB_CONTROL", "dj")
    text = render_panel(player)
    assert player.sid not in text and f"/{player.watch_id}" in text


# --- DJ role storage --------------------------------------------------------


def test_dj_role_is_saved_and_reloaded(data_dir):
    permissions.set_dj_role(GUILD_ID, DJ_ROLE)
    saved = json.loads((data_dir / "permissions.json").read_text())
    assert saved == {str(GUILD_ID): {"dj_role": DJ_ROLE}}
    state.guild_perms.clear()
    permissions.load_perms()
    assert permissions.dj_role(GUILD_ID) == DJ_ROLE
    permissions.set_dj_role(GUILD_ID, None)
    permissions.load_perms()
    assert permissions.dj_role(GUILD_ID) is None


def test_bad_permissions_file_entries_are_dropped(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "permissions.json").write_text(
        json.dumps({"x": {"dj_role": 1}, "2": {"dj_role": "a"}, "3": {"dj_role": 7}})
    )
    permissions.load_perms()
    assert state.guild_perms == {3: {"dj_role": 7}}


# --- Discord gate -----------------------------------------------------------


def test_defaults_let_everyone_run_every_command():
    for name in permissions.DJ_COMMANDS:
        assert permissions.can_run_dj_command(member(), GUILD_ID, name, None)


@pytest.mark.parametrize("name", sorted(permissions.DJ_COMMANDS))
def test_dj_role_gates_mutating_commands(name):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    check = permissions.can_run_dj_command
    assert not check(member(), GUILD_ID, name, None)
    assert check(member(roles=[DJ_ROLE]), GUILD_ID, name, None)
    assert check(member(manager=True), GUILD_ID, name, None)


@pytest.mark.parametrize("name", ["play", "list", "weblink", "help"])
def test_open_commands_stay_open(name):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    assert permissions.can_run_dj_command(member(), GUILD_ID, name, None)


async def test_requester_may_skip_own_track_only(player, playing):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    check = permissions.can_run_dj_command
    assert check(member(10), GUILD_ID, "next", player)
    assert not check(member(10), GUILD_ID, "pause", player)
    assert not check(member(11), GUILD_ID, "next", player)


async def test_global_check_raises_not_dj(channel):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    with pytest.raises(commands.NotDJ):
        await commands.dj_gate(guild_ctx(channel, member(), "stop"))
    assert await commands.dj_gate(guild_ctx(channel, member(), "play"))
    assert commands.dj_gate in commands.bot._checks


async def test_not_dj_error_message(channel):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    await commands.on_command_error(
        guild_ctx(channel, member(), "next"), commands.NotDJ()
    )
    assert "Only DJs can use `!next`" in channel.log[-1]
    assert "**DJ** role" in channel.log[-1] and "queued" in channel.log[-1]
    await commands.on_command_error(
        guild_ctx(channel, member(), "stop"), commands.NotDJ()
    )
    assert "queued" not in channel.log[-1]


# --- !djrole and !weblink ---------------------------------------------------


def test_djrole_needs_manage_guild():
    names = [c.__qualname__ for c in commands.djrole.checks]
    assert any(n.startswith("has_guild_permissions") for n in names)


async def test_djrole_rotates_a_public_control_link(player, channel, monkeypatch):
    role = types.SimpleNamespace(id=DJ_ROLE, name="DJ", is_default=lambda: False)

    async def convert(self, ctx, argument):
        return role

    monkeypatch.setattr(ext_commands.RoleConverter, "convert", convert)
    old_sid, watch = player.sid, player.watch_id
    ctx = guild_ctx(channel, member(manager=True), "djrole")
    await commands.djrole.callback(ctx, choice="DJ")
    assert permissions.dj_role(GUILD_ID) == DJ_ROLE
    assert resolve_link(old_sid) == (None, False)
    assert resolve_link(watch) == (player.sid, False)
    assert player.sid != old_sid and resolve_link(player.sid) == (player.sid, True)
    assert player.sid not in render_panel(player)

    await commands.djrole.callback(ctx, choice="off")
    assert permissions.dj_role(GUILD_ID) is None
    await settle()


async def test_every_djrole_change_rotates_and_retires_edit_links(
    player, channel, monkeypatch
):
    from djsnake.themes import check_edit_token, issue_edit_token

    roles = iter([DJ_ROLE, DJ_ROLE + 1])

    async def convert(self, ctx, argument):
        rid = next(roles)
        return types.SimpleNamespace(id=rid, name=f"R{rid}", is_default=lambda: False)

    monkeypatch.setattr(ext_commands.RoleConverter, "convert", convert)
    ctx = guild_ctx(channel, member(manager=True), "djrole")
    seen = {player.sid}
    for choice in ("DJ", "Other", "off"):
        token = issue_edit_token(GUILD_ID)
        await commands.djrole.callback(ctx, choice=choice)
        assert player.sid not in seen  # A new control id each time
        seen.add(player.sid)
        assert not check_edit_token(token, GUILD_ID)
    await settle()


async def test_weblink_refused_to_non_djs(player, channel, monkeypatch):
    monkeypatch.setattr(permissions, "WEB_CONTROL", "dj")
    author = member()
    with pytest.raises(commands.NotDJ):
        await commands.weblink.callback(guild_ctx(channel, author, "weblink"))
    assert author.dms == []


async def test_weblink_dms_both_links_to_a_dj(player, channel):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    author = member(roles=[DJ_ROLE])
    posted = len(channel.log)
    await commands.weblink.callback(guild_ctx(channel, author, "weblink"))
    assert len(author.dms) == 1
    assert f"/{player.sid}" in author.dms[0] and f"/{player.watch_id}" in author.dms[0]
    assert len(channel.log) == posted  # Nothing public


async def test_weblink_with_dm_closed(player, channel):
    author = member(dms_open=False)
    await commands.weblink.callback(guild_ctx(channel, author, "weblink"))
    assert "direct message" in channel.log[-1]
    assert player.sid not in channel.log[-1]
