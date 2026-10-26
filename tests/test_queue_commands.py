"""/remove, /move, /previous, /clear, /nowplaying and /history."""

import types

import pytest

from djsnake import commands, permissions, state
from djsnake.controls import ControlError
from djsnake.playback import play_next
from djsnake.player import push_history
from fakes import make_ctx, make_slash_ctx, make_track, settle, titles
from conftest import GUILD_ID
from test_permissions import DJ_ROLE, guild_ctx, member


@pytest.fixture
async def queue(player, ctx, media_dir):
    """A playing, then B, C, D queued (A and C queued by member 10)."""
    for name, who in (("A", 10), ("B", 11), ("C", 10), ("D", 12)):
        player.add_to_queue(
            make_track(media_dir, name, requester_id=who, requester=f"user{who}")
        )
    await play_next(ctx)
    return player


def run(channel, slash=False):
    make = make_slash_ctx if slash else make_ctx
    c = make(GUILD_ID, channel, member(10))
    c.command = types.SimpleNamespace(name="x")
    return c


# --- /remove ----------------------------------------------------------------


async def test_remove_by_position(queue, channel):
    await commands.remove.callback(run(channel), 2)
    assert titles(queue.queue) == ["B", "D"]
    assert channel.reactions == ["✅"]


async def test_remove_bounds(queue, channel):
    for bad in (0, 4, -1):
        c = run(channel, slash=True)
        await commands.remove.callback(c, bad)
        assert "Invalid position" in c.replies[-1][0] and c.replies[-1][1]
    assert titles(queue.queue) == ["B", "C", "D"]


async def test_remove_empty_queue(player, channel):
    c = run(channel, slash=True)
    await commands.remove.callback(c, 1)
    assert "queue is empty" in c.replies[-1][0]


async def test_remove_without_player(channel):
    c = run(channel, slash=True)
    await commands.remove.callback(c, 1)
    assert c.replies


async def test_remove_permissions(queue, channel):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    # Open to the gate for everyone: the command checks the track's owner.
    assert permissions.can_run_dj_command(member(11), GUILD_ID, "remove", queue)
    # Member 10 queued C (position 2) but not B (position 1).
    with pytest.raises(commands.NotDJ):
        await commands.remove.callback(guild_ctx(channel, member(10)), 1)
    assert titles(queue.queue) == ["B", "C", "D"]
    await commands.remove.callback(guild_ctx(channel, member(10)), 2)
    assert titles(queue.queue) == ["B", "D"]
    # A DJ removes anyone's track.
    await commands.remove.callback(guild_ctx(channel, member(99, [DJ_ROLE])), 1)
    assert titles(queue.queue) == ["D"]


def test_remove_denied_message_mentions_owner(channel):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    guild = guild_ctx(channel, member()).guild
    text = commands.error_message(commands.NotDJ(), "remove", guild=guild)
    assert "queued that track" in text


# --- /move ------------------------------------------------------------------


async def test_move_positions(queue, channel):
    await commands.move.callback(run(channel), 3, 1)
    assert titles(queue.queue) == ["D", "B", "C"]
    await commands.move.callback(run(channel), 1, 3)
    assert titles(queue.queue) == ["B", "C", "D"]


@pytest.mark.parametrize("args", [(0, 1), (1, 4), (4, 1), (1, 0)])
async def test_move_bounds(queue, channel, args):
    c = run(channel, slash=True)
    await commands.move.callback(c, *args)
    assert "Invalid position" in c.replies[-1][0]
    assert titles(queue.queue) == ["B", "C", "D"]


# --- /previous and /clear ---------------------------------------------------


async def test_previous_restarts_without_history(queue, channel):
    await commands.previous.callback(run(channel))
    assert queue.pending_action == "restart"


async def test_previous_nothing_playing(player, channel):
    c = run(channel, slash=True)
    await commands.previous.callback(c)
    assert c.replies and "No music is currently playing" in c.replies[-1][0]


async def test_clear_keeps_current_track(queue, channel):
    await commands.clear.callback(run(channel))
    assert queue.queue == [] and queue.now_playing["title"] == "A"
    assert channel.reactions == ["✅"]


async def test_clear_empty(queue, channel):
    queue.queue.clear()
    c = run(channel, slash=True)
    await commands.clear.callback(c)
    assert "queue is empty" in c.replies[-1][0]


def test_new_commands_are_dj_only():
    for name in ("previous", "move", "clear"):
        assert name in permissions.DJ_COMMANDS
    assert "remove" not in permissions.DJ_COMMANDS
    assert "remove" in permissions.OWN_TRACK_COMMANDS


@pytest.mark.parametrize("name", ["previous", "move", "clear"])
def test_dj_role_gates_new_commands(name):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    check = permissions.can_run_dj_command
    assert not check(member(), GUILD_ID, name, None)
    assert check(member(roles=[DJ_ROLE]), GUILD_ID, name, None)


# --- Autocomplete -----------------------------------------------------------


def interaction():
    return types.SimpleNamespace(guild=types.SimpleNamespace(id=GUILD_ID))


async def test_autocomplete_lists_positions_and_titles(queue):
    choices = await commands.remove_autocomplete(interaction(), "")
    assert [(c.name, c.value) for c in choices] == [
        ("1. B", 1),
        ("2. C", 2),
        ("3. D", 3),
    ]
    filtered = await commands.move_from_autocomplete(interaction(), "c")
    assert [c.value for c in filtered] == [2]
    assert await commands.move_to_autocomplete(interaction(), "")


async def test_autocomplete_cap_and_no_player(queue, media_dir):
    for i in range(40):
        queue.add_to_queue(make_track(media_dir, f"T{i}" + "x" * 150))
    choices = await commands.remove_autocomplete(interaction(), "")
    assert len(choices) == 25 and all(len(c.name) <= 100 for c in choices)
    state.music_players.clear()
    assert await commands.remove_autocomplete(interaction(), "") == []


def test_autocomplete_registered():
    for name, param in (("remove", "position"), ("move", "from_position")):
        assert state.bot.tree.get_command(name)._params[param].autocomplete


# --- /nowplaying ------------------------------------------------------------


def capture(channel):
    c = run(channel)
    sent = []

    async def send(content=None, **kwargs):
        sent.append(kwargs)

    c.send = send
    return c, sent


def fields_of(embed):
    return {f.name: f.value for f in embed.fields}


async def test_nowplaying_embed_fields(queue, channel, monkeypatch):
    queue.now_playing.update(thumbnail="https://img/x.jpg", start=0.0, end=100.0)
    queue.loop, queue.autoplay = "queue", True
    monkeypatch.setattr(type(queue), "elapsed", lambda self: 50.0)
    c, sent = capture(channel)
    await commands.nowplaying.callback(c)
    embed = sent[0]["embed"]
    assert embed.title == "A"
    assert embed.url == "https://www.youtube.com/watch?v=A"
    assert embed.thumbnail.url == "https://img/x.jpg"
    assert "0:50 / 1:40" in embed.description and "🔘" in embed.description
    fields = fields_of(embed)
    assert fields["Requested by"] == "user10"
    assert fields["Source"] == "YouTube"
    assert fields["Loop"] == "queue"
    assert fields["Autoplay"] == "On"
    assert fields["Volume"] == "100 %"
    assert fields["Clip page"].endswith(f"/{queue.sid}")


async def test_nowplaying_link_follows_control_rules(queue, channel):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    c, sent = capture(channel)
    await commands.nowplaying.callback(c)
    link = fields_of(sent[0]["embed"])["Clip page"]
    assert link.endswith(f"/{queue.watch_id}") and queue.sid not in link


async def test_nowplaying_source_and_missing_requester(queue, channel):
    queue.now_playing.update(url="https://soundcloud.com/a/b", requester=None)
    c, sent = capture(channel)
    await commands.nowplaying.callback(c)
    fields = fields_of(sent[0]["embed"])
    assert fields["Source"] == "SoundCloud"
    assert fields["Requested by"] == "the clip page"


async def test_nowplaying_nothing_playing(player, channel):
    c = run(channel, slash=True)
    await commands.nowplaying.callback(c)
    assert "No music is currently playing" in c.replies[-1][0]


def test_np_alias():
    assert "np" in state.bot.get_command("nowplaying").aliases


def test_progress_bar():
    assert commands.progress_bar(0, 100).startswith("🔘")
    assert commands.progress_bar(100, 100).endswith("🔘")
    assert commands.progress_bar(5, None) == "▬" * 15
    assert len(commands.progress_bar(50, 100)) == 15


# --- /history ---------------------------------------------------------------


async def test_history_empty(player, channel):
    c = run(channel, slash=True)
    await commands.history.callback(c, 10)
    assert "Nothing has been played" in c.replies[-1][0]


async def test_history_default_max_and_order(player, channel):
    for i in range(30):
        push_history(player, {"title": f"T{i}", "duration": 65, "requester": "bob"})
    assert len(player.played) == 25
    c = run(channel)
    await commands.history.callback(c)  # default count
    text = channel.log[-1]
    assert text.count("\n") == 10 and "1. T29 `1:05` — bob" in text
    await commands.history.callback(c, 99)
    assert channel.log[-1].count("\n") == 25 and "T5" in channel.log[-1]
    await commands.history.callback(c, 0)
    assert channel.log[-1].count("\n") == 1


async def test_history_truncates_under_limit(player, channel):
    for i in range(25):
        push_history(player, {"title": "y" * 300 + str(i), "requester": "z" * 90})
    await commands.history.callback(run(channel), 25)
    assert len(channel.log[-1]) < 2000


async def test_played_tracks_are_logged(queue):
    queue.voice_client.stop()
    await settle()
    assert queue.played and queue.played[-1]["title"] == "A"
    assert queue.played[-1]["requester"] == "user10"


def test_control_error_code():
    assert ControlError("queue_empty").status == 409


async def test_remove_and_move_actions_do_not_mention_the_web(queue, channel):
    await commands.remove.callback(run(channel), 2)
    assert queue.last_action == "🗑️ Removed: C"
    c = run(channel)
    c.author.guild_permissions.manage_guild = True
    await commands.move.callback(c, 2, 1)
    assert queue.last_action == "🔀 Moved D to position 1"
    assert "web" not in queue.last_action


async def test_history_survives_reset(player, ctx, monkeypatch):
    push_history(player, {"title": "Kept", "duration": 60, "requester": "bob"})
    ctx.guild.voice_client = None
    real_sleep = commands.asyncio.sleep
    monkeypatch.setattr(commands.asyncio, "sleep", lambda s: real_sleep(min(s, 0.01)))
    await commands.reset_voice.callback(ctx)
    monkeypatch.undo()
    fresh = state.music_players[GUILD_ID]
    assert fresh is not player
    assert [t["title"] for t in fresh.played] == ["Kept"]
