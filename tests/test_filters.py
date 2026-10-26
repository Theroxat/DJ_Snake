"""Audio filters: the FFmpeg chains, the playback rate, switching mid-track,
crossfade under a speed filter, permissions, the web action and the state."""

import shlex
import shutil
import subprocess
import time

import pytest

from djsnake import commands, filters, permissions, state, web
from djsnake.config import FILTER_PRESETS
from djsnake.controls import ControlError, control_filter, control_pause
from djsnake.media import playback_options
from djsnake.playback import open_track_source, play_next
from djsnake.player import MusicPlayer
from djsnake.sessions import render_panel
from fakes import make_track, settle
from test_permissions import DJ_ROLE, GUILD_ID, guild_ctx, member

FPS = 50
ANALYSED = {"start": 0.0, "end": 100.0, "loudnorm": None}


async def get_state(client, link):
    return await (await client.get(f"/api/{link}/state")).json()


async def post(client, player, **body):
    r = await client.post(f"/api/{player.sid}/control", json=body)
    return r.status, await r.json()


@pytest.fixture
async def playing(player, ctx, media_dir):
    """An analysed track A (0-100 s) playing, B queued."""
    a = make_track(media_dir, "A", **ANALYSED)
    b = make_track(media_dir, "B", **ANALYSED)
    player.add_to_queue(a)
    player.add_to_queue(b)
    await play_next(ctx)
    return a, b


def rewind(player, seconds):
    """Pretend the current segment has played `seconds` of wall time."""
    player.started_at -= seconds


# --- Chains --------------------------------------------------------------------


def test_off_adds_nothing():
    assert filters.chain("off") == ""
    assert filters.rate("off") == 1.0


@pytest.mark.parametrize("name", [p for p in FILTER_PRESETS if p not in ("off", "eq")])
def test_every_preset_has_a_chain_on_a_known_stereo_format(name):
    chain = filters.chain(name)
    assert chain.startswith("aresample=48000,aformat=channel_layouts=stereo,")
    assert " " not in chain  # FFmpegPCMAudio splits its options on spaces


@pytest.mark.parametrize(
    "name, expected",
    [
        ("bassboost", "bass=g=10"),
        ("nightcore", "asetrate=60000,aresample=48000"),
        ("vaporwave", "asetrate=38400,aresample=48000"),
        ("8d", "apulsator=hz=0.125"),
        ("karaoke", "c0=c0-c1|c1=c1-c0"),
        ("soft", "lowpass=f=3200"),
    ],
)
def test_preset_chains(name, expected):
    assert expected in filters.chain(name)


def test_only_speed_presets_change_the_rate():
    rates = {name: filters.rate(name) for name in FILTER_PRESETS}
    assert rates.pop("nightcore") == 1.25 and rates.pop("vaporwave") == 0.8
    assert set(rates.values()) == {1.0}
    # 60000 / 48000 and 38400 / 48000: the chain matches the rate
    assert 60000 / 48000 == filters.rate("nightcore")
    assert 38400 / 48000 == filters.rate("vaporwave")


def test_eq_builds_only_the_bands_that_move():
    assert filters.chain("eq", {"low": 0, "mid": 0, "high": 0}) == ""
    cut = filters.chain("eq", {"low": -6, "mid": 0, "high": 0})
    assert "bass=g=-6" in cut and "equalizer" not in cut and "treble" not in cut
    assert "alimiter" not in cut  # Cuts cannot clip
    boost = filters.chain("eq", {"low": 3, "mid": -2, "high": 12})
    assert "bass=g=3" in boost and "g=-2" in boost and "treble=g=12" in boost
    assert boost.endswith("alimiter=limit=0.9:level=false")


@pytest.mark.parametrize(
    "eq, ok",
    [
        ({"low": 12}, True),
        ({"low": -12, "mid": 0, "high": 5}, True),
        ({"low": 13}, False),
        ({"mid": 1.5}, False),
        ({"high": True}, False),
        ({"treble": 3}, False),
        ([], False),
    ],
)
def test_clean_eq(eq, ok):
    assert (filters.clean_eq(eq) is not None) == ok


@pytest.mark.parametrize(
    "text, name",
    [("Nightcore", "nightcore"), (" 8D ", "8d"), ("none", "off"), ("bass", "bassboost")]
    + [("echo", None), ("", None), (None, None)],
)
def test_parse_filter(text, name):
    assert filters.parse_filter(text) == name


def test_chain_goes_after_the_automix_fades():
    _, kw = playback_options(ANALYSED, 10.0, extra_filters=filters.chain("soft"))
    fades, _, extra = kw["options"].partition("afade=t=out:st=86.000:d=4.000,")
    assert "afade=t=in" in fades and extra == filters.chain("soft")
    assert kw["before_options"] == "-ss 10.000 -t 90.000"  # Still file time


def test_unanalysed_tracks_get_the_chain_too():
    _, kw = playback_options({}, 0, extra_filters="lowpass=f=3200")
    assert kw == {"options": "-vn -af lowpass=f=3200"}
    assert playback_options({}, 0)[1] == {"options": "-vn"}  # Unchanged off


@pytest.mark.parametrize("name", ["nightcore", "vaporwave"])
def test_speed_filters_scale_the_frame_counts(name, media_dir, ffmpeg):
    p = MusicPlayer()
    p.crossfade, p.filter = True, name
    track = make_track(media_dir, "X", start=0.0, end=20.0, loudnorm=None)
    offset, _, frames, fade_frames = open_track_source(p, track, 0.0, fade_in=4.0)
    rate = filters.rate(name)
    assert frames == round(20 / rate * FPS)  # Wall time of 20 s of file
    assert fade_frames == round(6 / rate * FPS)
    # The fade-in asked for is wall time; the filter sees file time
    assert f"afade=t=in:st=0:d={4.0 * rate:.3f}" in ffmpeg.last["options"]
    assert ffmpeg.last["options"].endswith(filters.chain(name))


# --- The clock -----------------------------------------------------------------


def test_elapsed_runs_at_the_rate_from_the_segment_start():
    p = MusicPlayer()
    p.rate, p.play_offset = 1.25, 10.0
    p.started_at = time.monotonic() - 10.0 - 8.0  # 8 s of wall time since 10 s
    assert 19.99 < p.elapsed() < 20.05  # 10 + 8 * 1.25
    assert 7.99 < p.played_seconds() < 8.05  # Stats count wall time
    p.rate = 1.0
    assert 17.99 < p.elapsed() < 18.05


# --- Switching mid-track -----------------------------------------------------------


async def test_switch_restarts_at_the_current_position(player, playing, ffmpeg):
    a, b = playing
    rewind(player, 30)
    played_before = list(player.voice_client.played)
    assert await control_filter(player, "nightcore") == "nightcore"
    await settle()
    assert player.now_playing is a and player.queue == [b]  # Same track, same queue
    assert player.voice_client.played == played_before + ["A.mp4"]
    assert ffmpeg.last["before_options"].startswith("-ss 30.")
    assert ffmpeg.last["options"].endswith(filters.chain("nightcore"))
    assert player.rate == 1.25 and 30 <= player.elapsed() < 30.5
    assert 29.9 < player.play_accum < 30.5  # The same play for the stats
    assert player.history == []


async def test_switch_while_paused_stays_paused(player, playing, ffmpeg):
    rewind(player, 12)
    await control_pause(player)
    await control_filter(player, "vaporwave")
    await settle()
    assert player.paused_at is not None and player.voice_client.is_paused()
    assert ffmpeg.last["before_options"].startswith("-ss 12.")
    assert 12 <= player.elapsed() < 12.5


async def test_same_chain_does_not_restart(player, playing):
    await control_filter(player, "eq", {"low": 0})  # Flat EQ = off
    await settle()
    assert player.voice_client.played == ["A.mp4"]
    assert player.filter == "eq"


async def test_eq_change_restarts_and_keeps_other_bands(player, playing, ffmpeg):
    await control_filter(player, "eq", {"low": 4, "high": -3})
    await settle()
    await control_filter(player, "eq", {"mid": 2})
    await settle()
    assert player.eq == {"low": 4, "mid": 2, "high": -3}
    assert ffmpeg.last["options"].endswith(filters.chain("eq", player.eq))
    assert len(player.voice_client.played) == 3


async def test_bad_filter_changes_nothing(player, playing):
    for name, eq in (("echo", None), ("eq", {"low": 20}), ("eq", "loud")):
        with pytest.raises(ControlError) as e:
            await control_filter(player, name, eq)
        assert e.value.code == "bad_filter" and e.value.status == 400
    assert player.filter == "off" and player.voice_client.played == ["A.mp4"]


async def test_out_of_voice_applies_to_the_next_track(player, ctx, media_dir, ffmpeg):
    player.voice_client.connected = False
    await control_filter(player, "soft")
    assert player.filter == "soft" and ffmpeg.calls == []
    player.voice_client.connected = True
    player.add_to_queue(make_track(media_dir, "C", **ANALYSED))
    await play_next(ctx)
    assert ffmpeg.last["options"].endswith(filters.chain("soft"))
    assert player.rate == 1.0


async def test_crossfade_under_nightcore(player, ctx, media_dir, ffmpeg):
    player.crossfade = True
    await control_filter(player, "nightcore")
    x = make_track(media_dir, "X", start=0.0, end=20.0, loudnorm=None)
    y = make_track(media_dir, "Y", start=0.0, end=30.0, loudnorm=None)
    player.add_to_queue(x)
    player.add_to_queue(y)
    await play_next(ctx)
    mixer = player.mixer
    for _ in range(800 - 240):  # 20 s at 1.25x is 800 frames, the tail 240
        mixer.read()
    await settle()
    assert player.now_playing is y and mixer.outgoing is not None
    # The overlap left (240 frames, 4.8 s of wall time) is 6 s of file
    assert "afade=t=in:st=0:d=6.000" in ffmpeg.last["options"]
    assert player.rate == 1.25


async def test_reset_keeps_the_filter(player, ctx, monkeypatch):
    await control_filter(player, "eq", {"high": 5})
    ctx.guild.voice_client = None
    real_sleep = commands.asyncio.sleep
    monkeypatch.setattr(commands.asyncio, "sleep", lambda s: real_sleep(min(s, 0.01)))
    await commands.reset_voice.callback(ctx)
    monkeypatch.undo()
    fresh = state.music_players[GUILD_ID]
    assert fresh is not player
    assert fresh.filter == "eq" and fresh.eq["high"] == 5


# --- State, web action and panel ------------------------------------------------------


async def test_state_carries_filter_and_rate(client, player, playing):
    s = await get_state(client, player.sid)
    assert (s["filter"], s["rate"]) == ("off", 1.0)
    assert s["eq"] == {"low": 0, "mid": 0, "high": 0}
    status, body = await post(client, player, action="filter", preset="vaporwave")
    assert (status, body) == (200, {"ok": True})
    await settle()
    s = await get_state(client, player.watch_id)  # The view-only page needs it too
    assert (s["filter"], s["rate"]) == ("vaporwave", 0.8)


async def test_state_rate_with_nothing_playing(client, player):
    await control_filter(player, "nightcore")
    s = await get_state(client, player.sid)
    assert s["now_playing"] is None and s["rate"] == 1.25


async def test_web_filter_sets_eq_and_announces(client, player, playing, channel):
    status, _ = await post(
        client, player, action="filter", preset="eq", eq={"low": 6, "mid": -2}
    )
    assert status == 200
    assert player.filter == "eq" and player.eq == {"low": 6, "mid": -2, "high": 0}
    await settle(0.1)
    panel = render_panel(player)
    assert "via the web page" in panel
    assert "Equaliser (low +6, mid -2, high +0 dB)" in panel


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"preset": 3},
        {"preset": "echo"},
        {"preset": "eq", "eq": {"low": 13}},
        {"preset": "eq", "eq": [1, 2, 3]},
        {"preset": "eq", "eq": {"bass": 1}},
    ],
)
async def test_web_filter_rejects_bad_input(client, player, playing, body):
    status, reply = await post(client, player, action="filter", **body)
    assert status == 400 and reply["ok"] is False
    assert player.filter == "off"


async def test_web_filter_works_out_of_voice(client, player):
    player.voice_client.connected = False
    status, _ = await post(client, player, action="filter", preset="8d")
    assert status == 200 and player.filter == "8d"


async def test_web_filter_is_a_control_action(client, player):
    assert "filter" in web.CONTROL_ACTIONS
    r = await client.post(
        f"/api/{player.watch_id}/control", json={"action": "filter", "preset": "8d"}
    )
    assert r.status == 403 and player.filter == "off"


async def test_panel_shows_the_filter_only_when_on(player):
    assert "Filter" not in render_panel(player)
    await control_filter(player, "nightcore")
    assert "🎛️ Filter: Nightcore (1.25× speed)" in render_panel(player)


# --- Command and permissions --------------------------------------------------------


async def test_filter_command_shows_and_sets(player, ctx, channel):
    await commands.filter_command.callback(ctx)
    assert "**off**" in channel.log[-1]
    await commands.filter_command.callback(ctx, "Bassboost")
    assert channel.reactions == ["✅"] and player.filter == "bassboost"
    await commands.filter_command.callback(ctx, "eq", 3, None, -4)
    assert player.eq == {"low": 3, "mid": 0, "high": -4}
    await commands.filter_command.callback(ctx)
    assert "low +3, mid +0, high -4" in channel.log[-1]


@pytest.mark.parametrize(
    "args", [("echo",), ("nightcore", 3), ("eq", 20), ("eq", None, -13)]
)
async def test_filter_command_rejects_bad_input(player, ctx, channel, args):
    await commands.filter_command.callback(ctx, *args)
    assert player.filter == "off"
    assert "Usage" in channel.log[-1] or "Unknown filter" in channel.log[-1]


def test_filter_needs_a_dj_only_to_set():
    assert "filter" in permissions.DJ_WHEN_SETTING
    assert "filter" not in permissions.DJ_COMMANDS


async def test_setting_a_filter_needs_a_dj(channel):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    ctx = guild_ctx(channel, member(), "filter")
    await commands.filter_command.callback(ctx)  # Showing is open
    with pytest.raises(commands.NotDJ):
        await commands.filter_command.callback(ctx, "nightcore")
    assert state.music_players[GUILD_ID].filter == "off"
    dj = guild_ctx(channel, member(roles=[DJ_ROLE]), "filter")
    await commands.filter_command.callback(dj, "nightcore")
    assert state.music_players[GUILD_ID].filter == "nightcore"


# --- Real ffmpeg ------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
@pytest.mark.ffmpeg
@pytest.mark.parametrize("channels", ["1", "2"])
@pytest.mark.parametrize("name", FILTER_PRESETS)
def test_real_ffmpeg_runs_every_chain_at_its_rate(tmp_path, name, channels):
    path = str(tmp_path / "t.m4a")
    tone = ["-f", "lavfi", "-i", "sine=f=440:d=12", "-ac", channels, path]
    subprocess.run(["ffmpeg", "-loglevel", "error", *tone], check=True)
    track = {"start": 1.0, "end": 11.0, "loudnorm": None}
    eq = {"low": 6, "mid": -3, "high": 4}
    _, kw = playback_options(track, 2.0, extra_filters=filters.chain(name, eq))
    out = subprocess.run(
        ["ffmpeg", *shlex.split(kw["before_options"]), "-i", path]
        + shlex.split(kw["options"])
        + ["-f", "s16le", "-ar", "48000", "-ac", "2", "-loglevel", "error", "-"],
        capture_output=True,
        check=True,
    )
    seconds = len(out.stdout) / (48000 * 4)
    assert abs(seconds - 9.0 / filters.rate(name)) < 0.1
