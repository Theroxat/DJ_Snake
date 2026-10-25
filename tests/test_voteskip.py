"""Vote to skip: threshold math, votes, recounts, bypasses, persistence."""

import json
import types

import pytest

from djsnake import app, commands, permissions, state, voteskip, web
from djsnake.controls import ControlError, control_voteskip, voteskip_update
from djsnake.playback import play_next
from djsnake.sessions import render_panel
from fakes import FakeAuthor, FakeChannel, make_track, settle

GUILD_ID = 1
DJ_ROLE = 555


def person(member_id, channel=None, deaf=False, bot=False, roles=(), manager=False):
    m = FakeAuthor()
    m.id, m.bot = member_id, bot
    m.guild = types.SimpleNamespace(id=GUILD_ID)
    m.display_name = f"member{member_id}"
    m.roles = [types.SimpleNamespace(id=r) for r in roles]
    m.guild_permissions = types.SimpleNamespace(manage_guild=manager)
    m.voice = types.SimpleNamespace(channel=channel, self_deaf=deaf, deaf=False)
    return m


def join(player, count, **kwargs):
    """Put `count` humans (ids 101..) in the bot's channel; returns them."""
    channel = player.voice_client.channel
    channel.members = getattr(channel, "members", [])
    people = [
        person(100 + i + len(channel.members), channel, **kwargs) for i in range(count)
    ]
    channel.members.extend(people)
    return people


def leave(player, member):
    player.voice_client.channel.members.remove(member)
    member.voice.channel = None


def guild_ctx(channel, author, command=None):
    role = types.SimpleNamespace(id=DJ_ROLE, name="DJ")
    guild = types.SimpleNamespace(
        id=GUILD_ID, name="Guild", get_role=lambda r: role if r == DJ_ROLE else None
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
    """A playing (requested by member 10), B queued, bot channel with no one."""
    player.voice_client.channel.members = []
    a = make_track(media_dir, "A", requester_id=10)
    b = make_track(media_dir, "B")
    player.add_to_queue(a)
    player.add_to_queue(b)
    await play_next(ctx)
    return a, b


# --- Threshold math ---------------------------------------------------------


@pytest.mark.parametrize(
    "total, percent, needed",
    [
        (1, 50, 1),
        (2, 50, 2),
        (3, 50, 2),
        (4, 50, 3),
        (5, 50, 3),
        (6, 50, 4),
        (10, 50, 6),
        (5, 100, 5),
        (5, 1, 1),
        (4, 25, 2),
        (10, 60, 7),
        (0, 50, 1),
    ],
)
def test_votes_needed_is_more_than_the_percent(total, percent, needed):
    assert voteskip.votes_needed(total, percent) == needed


@pytest.mark.parametrize("text, value", [("60", 60), ("60%", 60), ("100", 100)])
def test_parse_percent_accepts(text, value):
    assert voteskip.parse_percent(text) == value


@pytest.mark.parametrize("text", ["0", "101", "-5", "x", "", "5.5", "٣"])
def test_parse_percent_rejects(text):
    assert voteskip.parse_percent(text) is None


def test_default_threshold_comes_from_the_environment_default():
    assert voteskip.threshold_for(GUILD_ID) == 50


def test_env_percent_falls_back_on_bad_values(monkeypatch):
    from djsnake import config

    for raw, expected in [("70", 70), ("0", 50), ("abc", 50), ("", 50), ("101", 50)]:
        monkeypatch.setenv("VOTESKIP_PERCENT", raw)
        assert config._percent_env("VOTESKIP_PERCENT", 50) == expected


# --- Voting -----------------------------------------------------------------


async def test_votes_accumulate_then_skip(player, playing):
    voters = join(player, 5)
    for v in voters[:2]:
        count, needed, skipped = await control_voteskip(player, GUILD_ID, v)
        assert (needed, skipped) == (3, False)
    assert count == 2
    assert player.vote_message.content == "🗳️ Vote to skip: **2/3** votes"
    count, needed, skipped = await control_voteskip(player, GUILD_ID, voters[2])
    assert (count, needed, skipped) == (3, 3, True)
    await settle()
    assert player.now_playing["title"] == "B"
    # One message per track, edited in place, ending on the final line.
    assert len(player.text_channel.log) >= 1


async def test_one_message_edited_in_place(player, playing):
    voters = join(player, 5)
    channel = player.text_channel
    before = len(channel.log)
    for v in voters[:3]:
        await control_voteskip(player, GUILD_ID, v)
    assert len(channel.log) - before == 1  # Posted once, then edited
    assert channel.edits[-1] == "⏭️ Skipped by vote (3/3)"


async def test_double_vote_is_refused(player, playing):
    voters = join(player, 5)
    await control_voteskip(player, GUILD_ID, voters[0])
    with pytest.raises(ControlError) as e:
        await control_voteskip(player, GUILD_ID, voters[0])
    assert e.value.code == "vote_already"
    assert voteskip.status(player, GUILD_ID) == (1, 3)


async def test_vote_needs_the_bot_channel(player, playing):
    join(player, 3)
    outsider = person(900, channel=None)
    with pytest.raises(ControlError) as e:
        await control_voteskip(player, GUILD_ID, outsider)
    assert e.value.code == "vote_not_in_channel"
    elsewhere = person(901, channel=object())
    with pytest.raises(ControlError):
        await control_voteskip(player, GUILD_ID, elsewhere)


async def test_deafened_members_do_not_count_or_vote(player, playing):
    voters = join(player, 4)
    deaf = join(player, 2, deaf=True)
    assert voteskip.status(player, GUILD_ID) == (0, 3)  # 4 listeners
    with pytest.raises(ControlError) as e:
        await control_voteskip(player, GUILD_ID, deaf[0])
    assert e.value.code == "vote_deafened"
    assert voters  # Humans only: a bot in the channel is ignored too
    join_bot = person(7, player.voice_client.channel, bot=True)
    player.voice_client.channel.members.append(join_bot)
    assert voteskip.status(player, GUILD_ID)[1] == 3


async def test_no_vote_without_a_track(player):
    player.voice_client.channel.members = []
    voters = join(player, 2)
    with pytest.raises(ControlError) as e:
        await control_voteskip(player, GUILD_ID, voters[0])
    assert e.value.code == "nothing_playing"


# --- Recount on voice changes ---------------------------------------------------


async def test_leaving_removes_the_vote_and_may_reach_the_threshold(player, playing):
    voters = join(player, 5)
    await control_voteskip(player, GUILD_ID, voters[0])
    await control_voteskip(player, GUILD_ID, voters[1])
    assert voteskip.status(player, GUILD_ID) == (2, 3)
    # Two non-voters leave: 3 listeners, 2 needed, 2 votes -> skipped.
    leave(player, voters[3])
    leave(player, voters[4])
    voice = types.SimpleNamespace
    await app._recount_votes(
        voters[4], voice(channel=player.voice_client.channel), voice(channel=None)
    )
    await settle()
    assert player.now_playing["title"] == "B"


async def test_a_voter_leaving_drops_their_vote(player, playing):
    voters = join(player, 4)
    await control_voteskip(player, GUILD_ID, voters[0])
    leave(player, voters[0])
    assert voteskip.status(player, GUILD_ID) == (0, 2)
    voice = types.SimpleNamespace
    await app._recount_votes(
        voters[0], voice(channel=player.voice_client.channel), voice(channel=None)
    )
    assert player.vote_message.content == "🗳️ Vote to skip: **0/2** votes"
    assert voteskip.web_status(player, GUILD_ID) is None
    # Coming back restores nothing by itself but the id is still remembered.
    player.voice_client.channel.members.append(voters[0])
    voters[0].voice.channel = player.voice_client.channel
    assert voteskip.status(player, GUILD_ID)[0] == 1


async def test_joining_raises_the_bar(player, playing):
    voters = join(player, 2)
    await control_voteskip(player, GUILD_ID, voters[0])
    assert voteskip.status(player, GUILD_ID) == (1, 2)
    join(player, 2)
    assert voteskip.status(player, GUILD_ID) == (1, 3)


async def test_deafening_a_voter_and_a_listener(player, playing):
    voters = join(player, 3)
    await control_voteskip(player, GUILD_ID, voters[0])
    voters[2].voice.self_deaf = True  # A non-voter deafens: 2 listeners
    assert voteskip.status(player, GUILD_ID) == (1, 2)
    voters[0].voice.self_deaf = True  # The voter deafens: vote ignored
    assert voteskip.status(player, GUILD_ID) == (0, 1)


async def test_recount_ignores_other_channels_and_no_votes(player, playing):
    voters = join(player, 3)
    voice = types.SimpleNamespace
    other = voice(channel=object())
    await app._recount_votes(voters[0], other, other)
    assert player.vote_message is None
    await control_voteskip(player, GUILD_ID, voters[0])
    posts = len(player.text_channel.log)
    await app._recount_votes(voters[1], other, other)
    assert len(player.text_channel.log) == posts


# --- Track change -------------------------------------------------------------


async def test_a_new_track_resets_the_votes(player, playing):
    voters = join(player, 5)
    await control_voteskip(player, GUILD_ID, voters[0])
    assert player.votes and player.vote_message is not None
    player.voice_client.finish()  # The track ends by itself
    await settle()
    assert player.now_playing["title"] == "B"
    assert player.votes == set()
    assert player.vote_message is None
    assert voteskip.web_status(player, GUILD_ID) is None
    # The same member can vote again on the new track.
    count, _, _ = await control_voteskip(player, GUILD_ID, voters[0])
    assert count == 1


async def test_stop_clears_votes(player, playing):
    voters = join(player, 5)
    await control_voteskip(player, GUILD_ID, voters[0])
    player.stop_and_clear()
    assert player.votes == set()


# --- The command: bypasses, threshold -------------------------------------------


async def test_requester_skips_at_once(player, playing, channel):
    join(player, 5)
    requester = person(10, player.voice_client.channel)
    ctx = guild_ctx(channel, requester, "voteskip")
    permissions.set_dj_role(GUILD_ID, DJ_ROLE)
    await commands.voteskip.callback(ctx)
    await settle()
    assert player.now_playing["title"] == "B"
    assert player.votes == set()


async def test_dj_skips_at_once(player, playing, channel):
    join(player, 5)
    permissions.set_dj_role(GUILD_ID, DJ_ROLE)
    dj = person(20, player.voice_client.channel, roles=[DJ_ROLE])
    await commands.voteskip.callback(guild_ctx(channel, dj, "voteskip"))
    await settle()
    assert player.now_playing["title"] == "B"


async def test_member_votes_through_the_command(player, playing, channel):
    voters = join(player, 4)
    permissions.set_dj_role(GUILD_ID, DJ_ROLE)
    ctx = guild_ctx(channel, voters[0], "voteskip")
    await commands.voteskip.callback(ctx)
    assert player.votes == {voters[0].id}
    assert player.now_playing["title"] == "A"
    assert "1/3" in player.vote_message.content


async def test_double_vote_via_command_is_an_error_reply(player, playing, channel):
    voters = join(player, 4)
    permissions.set_dj_role(GUILD_ID, DJ_ROLE)
    ctx = guild_ctx(channel, voters[0], "voteskip")
    await commands.voteskip.callback(ctx)
    await commands.voteskip.callback(ctx)
    assert "already voted" in channel.log[-1]


async def test_threshold_is_set_by_djs_only_and_persisted(
    player, playing, channel, data_dir
):
    permissions.set_dj_role(GUILD_ID, DJ_ROLE)
    voters = join(player, 2)
    ctx = guild_ctx(channel, voters[0], "voteskip")
    with pytest.raises(commands.NotDJ):
        await commands.voteskip.callback(ctx, "threshold", "75")
    manager = person(30, player.voice_client.channel, manager=True)
    ctx = guild_ctx(channel, manager, "voteskip")
    await commands.voteskip.callback(ctx, "threshold", "75%")
    assert voteskip.threshold_for(GUILD_ID) == 75
    saved = json.loads((data_dir / "voteskip.json").read_text())
    assert saved == {str(GUILD_ID): 75}
    state.guild_voteskip.clear()
    voteskip.load_thresholds()
    assert voteskip.threshold_for(GUILD_ID) == 75
    await commands.voteskip.callback(ctx, "threshold", "0")
    assert voteskip.threshold_for(GUILD_ID) == 75
    assert "Usage" in channel.log[-1]
    await commands.voteskip.callback(ctx, "threshold")
    assert "75%" in channel.log[-1]


def test_load_drops_bad_entries(data_dir):
    data_dir.mkdir(exist_ok=True)
    (data_dir / "voteskip.json").write_text(
        json.dumps({"1": 60, "2": 0, "3": 101, "x": 50, "4": True, "5": "9"})
    )
    voteskip.load_thresholds()
    assert state.guild_voteskip == {1: 60}


async def test_threshold_changes_the_bar(player, playing):
    join(player, 4)
    voteskip.set_threshold(GUILD_ID, 100)
    assert voteskip.status(player, GUILD_ID)[1] == 4
    voteskip.set_threshold(GUILD_ID, 25)
    assert voteskip.status(player, GUILD_ID)[1] == 2


# --- Permission error hint ------------------------------------------------------


def test_next_refusal_suggests_voteskip_when_a_dj_role_is_set():
    guild = types.SimpleNamespace(
        id=GUILD_ID, get_role=lambda r: types.SimpleNamespace(id=r, name="DJ")
    )
    permissions.set_dj_role(GUILD_ID, DJ_ROLE)
    text = commands.error_message(commands.NotDJ(), "next", guild=guild)
    assert "`!voteskip`" in text
    text = commands.error_message(commands.NotDJ(), "next", slash=True, guild=guild)
    assert "`/voteskip`" in text
    other = commands.error_message(commands.NotDJ(), "pause", guild=guild)
    assert "voteskip" not in other


def test_no_hint_without_a_dj_role():
    guild = types.SimpleNamespace(id=GUILD_ID, get_role=lambda r: None)
    text = commands.error_message(commands.NotDJ(), "next", guild=guild)
    assert "voteskip" not in text


def test_hint_is_localised():
    from djsnake.i18n import t

    assert "voteskip" in t("cmd.not_dj_voteskip", "fr", prefix="!")


# --- Panel and web state --------------------------------------------------------


async def test_panel_and_state_show_the_count(player, playing, client):
    voters = join(player, 5)
    assert "votes" not in render_panel(player).lower()
    body = await (await client.get(f"/api/{player.sid}/state")).json()
    assert body["votes"] is None
    await control_voteskip(player, GUILD_ID, voters[0])
    assert "Skip votes 1/3" in render_panel(player)
    body = await (await client.get(f"/api/{player.sid}/state")).json()
    assert body["votes"] == {"count": 1, "needed": 3}


async def test_web_has_no_voting_action():
    assert "voteskip" not in web.CONTROL_ACTIONS


async def test_update_without_votes_does_not_skip(player, playing):
    join(player, 3)
    count, needed, skipped = await voteskip_update(player, GUILD_ID)
    assert (count, skipped) == (0, False)
    assert FakeChannel  # Imported for parity with other test modules
