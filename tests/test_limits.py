"""Anti-abuse queue limits: queue size, per member, track length, play
cooldown, DJ bypass, playlist reports, the web bucket and persistence. No
network: downloads are replaced by a recorder."""

import json
import os
import types

import pytest

from djsnake import commands, config, limits, media, state
from djsnake.controls import ControlError, control_add
from djsnake.playback import _find_autoplay, download_and_queue_song, queue_playlist
from fakes import FakeAuthor, make_ctx, make_slash_ctx, make_track

GUILD_ID = 1
DJ_ROLE = 555


def member(member_id=10, roles=(), manager=False):
    m = FakeAuthor()
    m.id = member_id
    m.display_name = f"member{member_id}"
    m.roles = [types.SimpleNamespace(id=r) for r in roles]
    m.guild_permissions = types.SimpleNamespace(manage_guild=manager)
    return m


def ctx_of(channel, author):
    return make_ctx(GUILD_ID, channel, author=author, name="Guild")


def set_limits(**values):
    for field, value in values.items():
        limits.set_limit(GUILD_ID, field, value)


@pytest.fixture
def downloads(monkeypatch, media_dir):
    """Fake yt-dlp: records each download; `lengths` maps a video id to its
    duration in seconds (default 100)."""
    fake = types.SimpleNamespace(urls=[], lengths={})

    def download_media(url, **kwargs):
        fake.urls.append(url)
        name = url.split("v=")[1]
        return make_track(media_dir, name, duration=fake.lengths.get(name, 100.0))

    monkeypatch.setattr(media, "download_media", download_media)
    return fake


@pytest.fixture
def busy(player):
    """A player that is already playing, so queued tracks stay queued."""
    player.is_playing = True
    return player


def queue_n(player, media_dir, n, requester_id=None, prefix="Q"):
    for i in range(n):
        player.add_to_queue(
            make_track(media_dir, f"{prefix}{i}", requester_id=requester_id)
        )


def url(name):
    return f"https://www.youtube.com/watch?v={name}"


# --- Defaults and values ----------------------------------------------------------


def test_env_defaults_apply_until_a_guild_overrides(monkeypatch):
    assert limits.limits_for(GUILD_ID) == {
        "queue_size": 200,
        "per_user": 0,
        "track_minutes": 0,
        "cooldown": 0,
    }
    monkeypatch.setattr(config, "MAX_TRACKS_PER_USER", 3)
    assert limits.limits_for(GUILD_ID)["per_user"] == 3
    set_limits(per_user=5)
    assert limits.limits_for(GUILD_ID)["per_user"] == 5
    assert limits.limits_for(2)["per_user"] == 3  # Other guilds keep the default
    set_limits(per_user=None)  # Back to the default
    assert limits.limits_for(GUILD_ID)["per_user"] == 3


@pytest.mark.parametrize(
    "field,text,expected",
    [
        ("queue_size", "50", 50),
        ("queue_size", "0", None),  # A queue of zero would refuse everything
        ("per_user", "0", 0),
        ("per_user", "-1", None),
        ("track_minutes", "abc", None),
        ("cooldown", "99999999", None),
        ("cooldown", "١٢", None),  # Non-ASCII digits
    ],
)
def test_parse_value(field, text, expected):
    assert limits.parse_value(field, text) == expected


# --- Queue size ---------------------------------------------------------------------


async def test_queue_size_limit_refuses_with_the_value(
    busy, channel, media_dir, downloads
):
    set_limits(queue_size=2)
    queue_n(busy, media_dir, 2)
    ctx = ctx_of(channel, member())
    track = await download_and_queue_song(ctx, busy, url("newtrack"))
    assert track is None
    assert downloads.urls == []  # Refused before downloading
    assert "queue is full (limit: 2)" in channel.log[-1]
    assert len(busy.queue) == 2


async def test_queue_size_applies_to_djs_too(busy, channel, media_dir, downloads):
    set_limits(queue_size=1)
    queue_n(busy, media_dir, 1)
    dj = member(manager=True)
    assert await download_and_queue_song(ctx_of(channel, dj), busy, url("x")) is None
    assert "queue is full" in channel.log[-1]


async def test_queue_fills_while_a_download_runs(busy, channel, media_dir, monkeypatch):
    """The check is repeated once the file is there: the queue may have filled."""
    set_limits(queue_size=1)
    ctx = ctx_of(channel, member())

    def download_media(url_, **kwargs):
        queue_n(busy, media_dir, 1, prefix="Racer")  # Someone else got in first
        return make_track(media_dir, "slow")

    monkeypatch.setattr(media, "download_media", download_media)
    assert await download_and_queue_song(ctx, busy, url("slow")) is None
    assert "queue is full" in channel.log[-1]
    assert not os.path.exists(os.path.join(media_dir, "slow.mp4"))  # Discarded


async def test_default_queue_size_is_200(busy, media_dir):
    queue_n(busy, media_dir, 199)
    assert limits.check_enqueue(busy, GUILD_ID, 10) is None
    queue_n(busy, media_dir, 1, prefix="last")
    reason, params = limits.check_enqueue(busy, GUILD_ID, 10)
    assert (reason, params) == ("queue_full", {"max": 200})


# --- Per member -----------------------------------------------------------------------


async def test_per_user_limit_counts_only_that_members_tracks(
    busy, channel, media_dir, downloads
):
    set_limits(per_user=2)
    queue_n(busy, media_dir, 2, requester_id=10, prefix="A")
    queue_n(busy, media_dir, 3, requester_id=11, prefix="B")
    alice, bob = member(10), member(11)
    assert await download_and_queue_song(ctx_of(channel, alice), busy, url("a")) is None
    assert "limit of 2 queued tracks" in channel.log[-1]
    carol = member(12)
    assert await download_and_queue_song(ctx_of(channel, carol), busy, url("c"))
    # Bob is over the limit too, whatever the others do.
    assert await download_and_queue_song(ctx_of(channel, bob), busy, url("b")) is None


async def test_per_user_limit_frees_up_as_tracks_play(busy, media_dir):
    set_limits(per_user=1)
    queue_n(busy, media_dir, 1, requester_id=10)
    assert limits.check_enqueue(busy, GUILD_ID, 10)[0] == "user_full"
    busy.next_in_queue()
    assert limits.check_enqueue(busy, GUILD_ID, 10) is None


@pytest.mark.parametrize(
    "dj",
    [
        member(20, manager=True),
        member(21, roles=[DJ_ROLE]),
    ],
    ids=["manager", "dj-role"],
)
async def test_djs_bypass_the_per_user_limit(busy, channel, media_dir, downloads, dj):
    from djsnake import permissions

    permissions.set_dj_role(GUILD_ID, DJ_ROLE)
    set_limits(per_user=1)
    queue_n(busy, media_dir, 3, requester_id=dj.id)
    track = await download_and_queue_song(ctx_of(channel, dj), busy, url("dj"))
    assert track is not None


async def test_without_a_dj_role_only_managers_bypass(busy, media_dir):
    set_limits(per_user=1)
    queue_n(busy, media_dir, 1, requester_id=10)
    ordinary = member(10)  # Everyone is a DJ without a role, but not here
    assert not limits.bypasses(ordinary, GUILD_ID)
    assert limits.bypasses(member(11, manager=True), GUILD_ID)


async def test_a_member_without_the_role_does_not_bypass(busy, media_dir):
    from djsnake import permissions

    permissions.set_dj_role(GUILD_ID, DJ_ROLE)
    assert not limits.bypasses(member(30), GUILD_ID)


# --- Track length ----------------------------------------------------------------------


async def test_metadata_duration_is_checked_before_downloading(
    busy, channel, downloads
):
    set_limits(track_minutes=5)
    ctx = ctx_of(channel, member())
    track = await download_and_queue_song(ctx, busy, url("long"), duration=7 * 60)
    assert track is None
    assert downloads.urls == []
    assert "lasts 7 min; the limit is 5 min" in channel.log[-1]


async def test_duration_is_checked_after_download_when_unknown(
    busy, channel, media_dir, downloads
):
    set_limits(track_minutes=5)
    downloads.lengths["huge"] = 20 * 60
    ctx = ctx_of(channel, member())
    assert await download_and_queue_song(ctx, busy, url("huge")) is None
    assert downloads.urls == [url("huge")]
    assert "lasts 20 min" in channel.log[-1]
    assert not os.path.exists(os.path.join(media_dir, "huge.mp4"))  # Discarded
    assert busy.queue == []


async def test_exactly_the_limit_and_unknown_lengths_pass(busy, channel, downloads):
    set_limits(track_minutes=5)
    downloads.lengths["edge"] = 300.0
    ctx = ctx_of(channel, member())
    assert await download_and_queue_song(ctx, busy, url("edge"))
    assert limits.check_enqueue(busy, GUILD_ID, 10, duration=None) is None


async def test_duration_limit_applies_to_djs(busy, channel, downloads):
    set_limits(track_minutes=1)
    ctx = ctx_of(channel, member(manager=True))
    assert await download_and_queue_song(ctx, busy, url("x"), duration=600) is None


async def test_no_duration_limit_by_default(busy, channel, downloads):
    ctx = ctx_of(channel, member())
    assert await download_and_queue_song(ctx, busy, url("x"), duration=36000)


# --- Playlists -------------------------------------------------------------------------


def flat(n, **fields):
    entry = {"id": f"vid{n:08d}", "title": f"Song {n}", "duration": 100}
    entry["url"] = url(entry["id"])
    entry.update(fields)
    return entry


async def test_playlist_queues_up_to_the_limit_and_reports_once(
    busy, channel, downloads
):
    set_limits(queue_size=3)
    ctx = ctx_of(channel, member())
    await queue_playlist(ctx, busy, [flat(i) for i in range(5)])
    assert len(busy.queue) == 3
    assert len(downloads.urls) == 3  # The rest never downloaded
    assert busy.load_final
    assert busy.load_line == ("✅ 3/5 tracks loaded (2 skipped: queue limit (3))")
    assert not [m for m in channel.log if "limit" in m]  # No message per track


async def test_playlist_skips_per_user_overflow(busy, channel, downloads):
    set_limits(per_user=2)
    ctx = ctx_of(channel, member())
    await queue_playlist(ctx, busy, [flat(i) for i in range(4)])
    assert len(busy.queue) == 2
    assert "2 skipped: limit per member (2)" in busy.load_line


async def test_playlist_skips_long_tracks_from_metadata_and_after_download(
    busy, channel, downloads
):
    set_limits(track_minutes=3)
    downloads.lengths["vid00000002"] = 400.0  # Unknown in the flat listing
    entries = [
        flat(0),
        flat(1, duration=900),  # Known: never downloaded
        flat(2, duration=None),  # Unknown: found out after the download
        flat(3),
    ]
    await queue_playlist(ctx_of(channel, member()), busy, entries)
    assert [t["title"] for t in busy.queue] == ["vid00000000", "vid00000003"]
    assert "vid00000001" not in " ".join(downloads.urls)
    assert "2 skipped: longer than 3 min" in busy.load_line


async def test_playlist_report_mixes_reasons_with_other_failures(
    busy, channel, downloads, monkeypatch
):
    set_limits(queue_size=1)
    ctx = ctx_of(channel, member())
    entries = [{"title": "no url"}, flat(0), flat(1)]
    await queue_playlist(ctx, busy, entries)
    assert busy.load_line == (
        "✅ 1/3 tracks loaded (1 skipped: queue limit (1), 1 unavailable)"
    )


async def test_spotify_entry_uses_its_metadata_duration(busy, channel, downloads):
    set_limits(track_minutes=3)
    entry = {"spotify": {"title": "Epic", "artists": ["A"], "duration": 900.0}}
    await queue_playlist(ctx_of(channel, member()), busy, [entry])
    assert busy.queue == [] and downloads.urls == []
    assert "1 skipped: longer than 3 min" in busy.load_line


# --- Web page bucket ------------------------------------------------------------------


async def test_web_adds_share_one_bucket(busy, media_dir):
    set_limits(per_user=2)
    queue_n(busy, media_dir, 2)  # requester_id None: added from the page
    with pytest.raises(ControlError) as e:
        await control_add(busy, GUILD_ID, "https://youtu.be/dQw4w9WgXcQ")
    assert e.value.code == "user_full" and e.value.status == 409
    assert "limit of 2 queued tracks" in e.value.text()
    # Discord members' tracks do not count against the page.
    busy.queue.clear()
    queue_n(busy, media_dir, 2, requester_id=10)
    assert limits.check_enqueue(busy, GUILD_ID, limits.WEB_BUCKET) is None


async def test_web_add_refused_when_the_queue_is_full(client, busy, media_dir):
    set_limits(queue_size=1)
    queue_n(busy, media_dir, 1, requester_id=10)
    r = await client.post(
        f"/api/{busy.sid}/control",
        json={"action": "add", "url": "https://youtu.be/dQw4w9WgXcQ"},
    )
    body = await r.json()
    assert r.status == 409
    assert body["error"] == "The queue is full (limit: 1)."


async def test_web_add_checks_the_duration_once_known(busy, channel, downloads):
    set_limits(track_minutes=2)
    from djsnake.controls import WebContext

    ctx = WebContext(GUILD_ID, busy)
    downloads.lengths["web"] = 600.0
    assert await download_and_queue_song(ctx, busy, url("web")) is None
    assert busy.queue == []


async def test_web_bucket_is_never_a_dj(busy, media_dir, downloads, channel):
    from djsnake.controls import WebContext

    set_limits(per_user=1)
    ctx = WebContext(GUILD_ID, busy)
    assert await download_and_queue_song(ctx, busy, url("w1"), skip_voice_check=True)
    assert await download_and_queue_song(ctx, busy, url("w2")) is None


# --- Autoplay ----------------------------------------------------------------------------


async def test_autoplay_only_obeys_the_queue_size(busy, media_dir, monkeypatch):
    set_limits(queue_size=1, per_user=1, track_minutes=1)
    calls = []

    def related_tracks(video_id):
        calls.append(video_id)
        return [
            {
                "id": "vid00000001",
                "title": "Song",
                "duration": 500,
                "channel": "c",
                "live_status": None,
            }
        ]

    def download_media(url_, **kwargs):
        return make_track(media_dir, "auto", duration=500.0)

    monkeypatch.setattr(media, "related_tracks", related_tracks)
    monkeypatch.setattr(media, "download_media", download_media)
    # Under the size limit: longer than MAX_TRACK_MINUTES is fine for autoplay.
    track = await _find_autoplay(busy, "AAAAAAAAAAA", busy.epoch)
    assert track is not None and track["autoplay"]
    queue_n(busy, media_dir, 1)  # Now the queue is full
    assert await _find_autoplay(busy, "AAAAAAAAAAA", busy.epoch) is None


# --- Cooldown --------------------------------------------------------------------------


@pytest.fixture
def play_env(monkeypatch):
    seen = []

    async def join(ctx):
        return True

    async def queue(ctx, player, url_, **kwargs):
        seen.append(url_)

    monkeypatch.setattr(commands, "join_voice_channel", join)
    monkeypatch.setattr(commands, "download_and_queue_song", queue)
    monkeypatch.setattr(media, "get_playlist_info", lambda u: [])
    return seen


async def play(channel, author, clock=None):
    ctx = make_slash_ctx(GUILD_ID, channel, author)
    await commands.play.callback(ctx, query="https://youtu.be/dQw4w9WgXcQ")
    return ctx


async def test_cooldown_blocks_a_second_play_until_it_expires(
    channel, play_env, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(state, "clock", lambda: now[0])
    set_limits(cooldown=30)
    alice = member(10)
    await play(channel, alice)
    assert len(play_env) == 1
    now[0] += 10
    ctx = await play(channel, alice)
    assert len(play_env) == 1
    text, private = ctx.replies[-1]
    assert private and "wait 20 s" in text
    now[0] += 20  # Exactly 30 s after the first
    await play(channel, alice)
    assert len(play_env) == 2


async def test_cooldown_is_per_member(channel, play_env, monkeypatch):
    monkeypatch.setattr(state, "clock", lambda: 1000.0)
    set_limits(cooldown=30)
    await play(channel, member(10))
    await play(channel, member(11))
    assert len(play_env) == 2


async def test_djs_skip_the_cooldown(channel, play_env, monkeypatch):
    monkeypatch.setattr(state, "clock", lambda: 1000.0)
    set_limits(cooldown=30)
    manager = member(10, manager=True)
    await play(channel, manager)
    await play(channel, manager)
    assert len(play_env) == 2


async def test_invalid_requests_do_not_start_the_cooldown(
    channel, play_env, monkeypatch
):
    monkeypatch.setattr(state, "clock", lambda: 1000.0)
    set_limits(cooldown=30)
    alice = member(10)
    ctx = make_slash_ctx(GUILD_ID, channel, alice)
    await commands.play.callback(ctx, query=None)  # Usage error
    await play(channel, alice)
    assert len(play_env) == 1


async def test_no_cooldown_by_default(channel, play_env):
    alice = member(10)
    await play(channel, alice)
    await play(channel, alice)
    assert len(play_env) == 2


def test_cooldown_remaining_rounds_up(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(state, "clock", lambda: now[0])
    set_limits(cooldown=5)
    limits.start_cooldown(GUILD_ID, 10)
    now[0] += 0.5
    assert limits.cooldown_remaining(GUILD_ID, 10) == 5
    now[0] += 4.5
    assert limits.cooldown_remaining(GUILD_ID, 10) == 0


# --- /limits and persistence ------------------------------------------------------------


def limits_file(data_dir):
    return data_dir / "limits.json"


async def test_limits_without_argument_shows_an_embed(channel):
    ctx = make_slash_ctx(GUILD_ID, channel, member())
    sent = {}

    async def send(content=None, **kwargs):
        sent.update(kwargs)

    ctx.send = send
    await commands.limits_command.callback(ctx)
    embed = sent["embed"]
    assert sent["ephemeral"] is True
    fields = {f.name: f.value for f in embed.fields}
    assert fields == {
        "Queue size": "200 tracks",
        "Per member": "Unlimited",
        "Track length": "Unlimited",
        "Play cooldown": "Off",
    }


async def test_a_manager_changes_a_limit_and_it_is_saved(channel, data_dir):
    ctx = make_slash_ctx(GUILD_ID, channel, member(manager=True))
    await commands.limits_command.callback(ctx, "user", "3")
    assert limits.limits_for(GUILD_ID)["per_user"] == 3
    assert json.loads(limits_file(data_dir).read_text()) == {
        str(GUILD_ID): {"per_user": 3}
    }
    await commands.limits_command.callback(ctx, "user", "default")
    assert json.loads(limits_file(data_dir).read_text()) == {}


async def test_others_cannot_change_a_limit(channel, data_dir):
    ctx = make_slash_ctx(GUILD_ID, channel, member())
    await commands.limits_command.callback(ctx, "queue", "5")
    assert "Manage Server" in ctx.replies[-1][0]
    assert limits.limits_for(GUILD_ID)["queue_size"] == 200
    assert not limits_file(data_dir).exists()


@pytest.mark.parametrize(
    "setting,value", [("queue", "0"), ("queue", "x"), ("bogus", "1"), ("queue", None)]
)
async def test_bad_arguments_get_the_usage(channel, setting, value):
    ctx = make_slash_ctx(GUILD_ID, channel, member(manager=True))
    await commands.limits_command.callback(ctx, setting, value)
    assert "Usage" in ctx.replies[-1][0]
    assert limits.limits_for(GUILD_ID)["queue_size"] == 200


def test_limits_survive_a_restart(data_dir):
    set_limits(queue_size=50, per_user=2, track_minutes=10, cooldown=15)
    state.guild_limits.clear()
    limits.load_limits()
    assert limits.limits_for(GUILD_ID) == {
        "queue_size": 50,
        "per_user": 2,
        "track_minutes": 10,
        "cooldown": 15,
    }


def test_bad_entries_are_dropped_on_load(data_dir):
    limits_file(data_dir).parent.mkdir(parents=True, exist_ok=True)
    limits_file(data_dir).write_text(
        json.dumps(
            {
                "1": {"queue_size": 0, "per_user": 2, "cooldown": True, "x": 1},
                "bad": {"per_user": 1},
                "2": "oops",
            }
        )
    )
    limits.load_limits()
    assert state.guild_limits == {1: {"per_user": 2}}


def test_an_unreadable_file_is_ignored(data_dir):
    limits_file(data_dir).parent.mkdir(parents=True, exist_ok=True)
    limits_file(data_dir).write_text("{not json")
    limits.load_limits()
    assert state.guild_limits == {}


def test_a_failed_save_restores_the_previous_value(monkeypatch):
    set_limits(per_user=2)

    def broken(*args):
        raise OSError("read-only")

    monkeypatch.setattr(limits, "_write_atomic", broken)
    with pytest.raises(OSError):
        limits.set_limit(GUILD_ID, "per_user", 9)
    assert limits.limits_for(GUILD_ID)["per_user"] == 2
