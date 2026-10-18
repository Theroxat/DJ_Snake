"""Listening stats: recording from the playback chain, the skip rule, artist
cleanup, the summary's ranges and guild scoping, the stats route, and the
live viewer count."""

import time
import types

import pytest

from djsnake import commands, state, stats, web
from djsnake.controls import control_jump, control_next, control_previous
from djsnake.playback import play_next, queue_playlist
from fakes import make_ctx, make_track, settle

from conftest import GUILD_ID


def plays(guild_id=GUILD_ID):
    stats.flush()
    conn = stats._connect(stats.DB_PATH)
    try:
        conn.row_factory = lambda cursor, row: dict(
            zip([c[0] for c in cursor.description], row)
        )
        return conn.execute(
            "SELECT * FROM plays WHERE guild_id = ? ORDER BY id", (guild_id,)
        ).fetchall()
    finally:
        conn.close()


def played_for(player, seconds):
    """Pretend the current track has been playing for `seconds`."""
    player.started_at -= seconds


async def start(player, ctx, media_dir, *names, **fields):
    made = [make_track(media_dir, n, **fields) for n in names]
    for track in made:
        player.add_to_queue(track)
    await play_next(ctx)
    return made


# --- Artist cleanup --------------------------------------------------------------


@pytest.mark.parametrize(
    "channel, title, expected",
    [
        ("Daft Punk - Topic", "Harder", "Daft Punk"),
        ("TaylorSwiftVEVO", "Shake It Off", "Taylor Swift"),
        ("Adele VEVO", "Hello", "Adele"),
        ("Stromae", "Alors on danse", "Stromae"),
        (None, "Queen - Bohemian Rhapsody", "Queen"),
        ("", "No separator here", None),
        (" - Topic", "Air - La femme d'argent", "Air"),
    ],
)
def test_guess_artist(channel, title, expected):
    assert stats.guess_artist(channel, title) == expected


def test_spotify_artist_wins():
    track = {"artist": "Justice", "channel": "Ed Banger Records", "title": "D.A.N.C.E"}
    assert stats.artist_of(track) == "Justice"


# --- Recording from the playback chain -------------------------------------------


async def test_natural_end_is_recorded_not_skipped(player, ctx, media_dir):
    a, b = await start(
        player, ctx, media_dir, "A", "B", video_id="vid", channel="Air - Topic"
    )
    a["requester"] = "Alice"
    played_for(player, 3)
    player.voice_client.finish()  # A ran out by itself, after 3 s
    await settle()
    assert player.now_playing is b
    [row] = plays()
    assert row["title"] == "A" and row["artist"] == "Air"
    assert row["requester"] == "Alice" and row["video_id"] == "vid"
    assert row["skipped"] == 0 and 2.9 <= row["played_seconds"] <= 3.5
    assert abs(row["started_at"] - time.time()) < 60


async def test_next_before_30s_is_a_skip_after_is_not(player, ctx, media_dir):
    await start(player, ctx, media_dir, "A", "B", "C")
    played_for(player, 10)
    await control_next(player)
    await settle()
    played_for(player, 45)
    await control_next(player)
    await settle()
    rows = plays()
    assert [(r["title"], r["skipped"]) for r in rows] == [("A", 1), ("B", 0)]


async def test_pauses_do_not_count_as_play(player, ctx, media_dir):
    await start(player, ctx, media_dir, "A", "B")
    played_for(player, 120)
    player.paused_total = 100.0  # 120 s since start, 100 of them paused
    await control_next(player)
    await settle()
    [row] = plays()
    assert row["skipped"] == 1 and 19 <= row["played_seconds"] <= 21


async def test_seek_keeps_counting_the_same_play(player, ctx, media_dir):
    await start(player, ctx, media_dir, "A", "B")
    played_for(player, 20)
    player.pending_action = "seek"
    player.seek_position, player.seek_paused = 80.0, False
    player.voice_client.stop()
    await settle()
    assert player.now_playing["title"] == "A" and plays() == []
    played_for(player, 15)  # 20 s, a jump to 80 s, then 15 s more
    await control_next(player)
    await settle()
    [row] = plays()
    assert row["skipped"] == 0 and 34 <= row["played_seconds"] <= 36  # Not 95


async def test_jump_and_previous_are_skips(player, ctx, media_dir):
    _, _, c = await start(player, ctx, media_dir, "A", "B", "C")
    await control_jump(player, GUILD_ID, c["id"])
    await settle()
    assert player.now_playing is c
    await control_previous(player)  # Within 5 s: back to A
    await settle()
    assert [(r["title"], r["skipped"]) for r in plays()] == [("A", 1), ("C", 1)]


async def test_stop_records_the_current_track(player, ctx, media_dir):
    await start(player, ctx, media_dir, "A", "B")
    played_for(player, 5)
    await commands.stop.callback(ctx)
    [row] = plays()
    assert row["title"] == "A" and row["skipped"] == 1


async def test_crossfade_promotion_records_the_old_track(player, ctx, media_dir):
    from djsnake.playback import _start_crossfade

    a, b = await start(player, ctx, media_dir, "A", "B", start=0.0, end=60.0)
    player.crossfade = True
    mixer = player.mixer
    mixer.remaining_seconds = lambda: 5.0
    mixer.attach = lambda *args: True
    played_for(player, 55)
    await _start_crossfade(player, mixer, player.play_gen)
    assert player.now_playing is b
    [row] = plays()
    assert row["title"] == "A" and row["skipped"] == 0
    assert 54 <= row["played_seconds"] <= 56


async def test_requester_and_spotify_artist(player, ctx, media_dir, monkeypatch):
    from djsnake import media, spotify

    def fake_download(url):
        return make_track(media_dir, url[-1], channel="Label Records")

    monkeypatch.setattr(media, "download_media", fake_download)
    monkeypatch.setattr(spotify, "find_youtube_match", lambda t: "https://y/x/S")
    author = types.SimpleNamespace(display_name="Bob", voice=None)
    bob_ctx = make_ctx(GUILD_ID, ctx.channel, author=author)
    await queue_playlist(
        bob_ctx,
        player,
        [{"spotify": {"title": "Song", "artists": ["Daft Punk"], "duration": 1}}],
    )
    track = player.now_playing
    assert track["requester"] == "Bob" and track["artist"] == "Daft Punk"


async def test_web_adds_have_no_requester(player, ctx, media_dir, monkeypatch):
    from djsnake import media
    from djsnake.controls import WebContext
    from djsnake.playback import download_and_queue_song

    monkeypatch.setattr(media, "download_media", lambda url: make_track(media_dir, "W"))
    web_ctx = WebContext(GUILD_ID, player)
    track = await download_and_queue_song(web_ctx, player, "u", skip_voice_check=True)
    assert track["requester"] is None


# --- Summary: ranges, scoping, aggregates ----------------------------------------

DAY = 86400
NOW = 1_760_000_000.0  # A Thursday (2025-10-09 08:53 UTC)


def add_play(guild, title, ago, requester="Alice", artist="X", skipped=False, vid=None):
    track = {
        "title": title,
        "artist": artist,
        "video_id": vid or title,
        "duration": 200,
        "requester": requester,
    }
    stats.record_play(
        guild, track, NOW - ago, 20 if skipped else 180, "next" if skipped else None
    )


@pytest.fixture
def history():
    add_play(1, "Old", 60 * DAY, requester="Carol", artist="Y")
    add_play(1, "Month", 20 * DAY, artist="Y")
    add_play(1, "Hit", 2 * DAY)
    add_play(1, "Hit", 1 * DAY, requester=None)
    add_play(1, "Hit", 3600)
    add_play(1, "Meh", 3600, skipped=True)
    stats.record_play(
        1, {"title": "Meh", "video_id": "Meh"}, NOW - 60, 2.0, "next"
    )  # Skipped again
    add_play(2, "Other guild", 3600, requester="Mallory", artist="Z")
    stats.record_session(1, NOW - 3600)
    stats.record_session(1, NOW - 40 * DAY)
    stats.record_session(2, NOW - 60)
    stats.record_listeners(1, 3, NOW - 40 * DAY)
    stats.record_listeners(1, 2, NOW - 3600)
    stats.record_listeners(1, 1, NOW - 60)  # Not a new daily peak
    stats.flush()


def test_ranges_filter_by_start_time(history):
    week = stats.summary(1, "week", now=NOW)
    month = stats.summary(1, "month", now=NOW)
    every = stats.summary(1, "all", now=NOW)
    assert week["totals"]["tracks"] == 5
    assert month["totals"]["tracks"] == 6
    assert every["totals"]["tracks"] == 7
    assert [t["title"] for t in every["top_tracks"]] == ["Hit", "Month", "Old"]
    assert every["top_tracks"][0]["plays"] == 3  # Skips are not counted
    assert week["totals"]["sessions"] == 1 and every["totals"]["sessions"] == 2
    assert every["totals"]["hours"] == round((5 * 180 + 20 + 2) / 3600, 1)


def test_guild_scoping(history):
    other = stats.summary(2, "all", now=NOW)
    assert [t["title"] for t in other["top_tracks"]] == ["Other guild"]
    assert other["top_djs"] == [{"name": "Mallory", "plays": 1}]
    assert other["totals"]["sessions"] == 1 and other["peak_listeners"] is None
    assert stats.summary(3, "all", now=NOW) == stats.empty_summary("all")


def test_djs_artists_skips_and_peak(history):
    week = stats.summary(1, "week", now=NOW)
    assert week["top_djs"] == [
        {"name": "Alice", "plays": 3},
        {"name": None, "plays": 2},  # The web page
    ]
    assert week["dj_of_week"] == {"name": "Alice", "plays": 3}
    assert week["top_artists"] == [{"name": "X", "plays": 3}]
    assert week["most_skipped"]["title"] == "Meh"
    assert week["most_skipped"]["skips"] == 2
    assert week["peak_listeners"] == {"count": 2, "at": NOW - 3600}
    assert stats.summary(1, "all", now=NOW)["peak_listeners"]["count"] == 3


def test_heatmap_is_utc_hour_of_week(history):
    heat = stats.summary(1, "week", now=NOW)["heatmap"]
    assert len(heat) == 168 and sum(heat) == 5
    # NOW - 3600 is Thursday 07:53 UTC (Hit, Meh): Monday is 0, so 3 * 24 + 7.
    assert heat[3 * 24 + 7] == 2 and heat[3 * 24 + 8] == 1


def test_missing_database_gives_an_empty_summary():
    assert stats.summary(1, "week") == stats.empty_summary("week")


def test_write_failure_never_raises(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(stats, "DB_PATH", str(blocker / "stats.db"))
    stats.record_play(1, {"title": "A"}, None, 10, None)
    stats.record_session(1)
    stats.flush()  # Printed and dropped


# --- Stats route ------------------------------------------------------------------


async def test_stats_route(client, player, ctx, media_dir):
    await start(player, ctx, media_dir, "A", "B")
    played_for(player, 40)
    await control_next(player)
    await settle()
    stats.flush()
    r = await client.get(f"/api/{player.sid}/stats?range=week")
    assert r.status == 200
    body = await r.json()
    assert body["range"] == "week" and body["totals"]["tracks"] == 1
    assert body["top_tracks"][0]["title"] == "A"
    assert body["totals"]["sessions"] == 0  # The fixture joined without a join

    assert (await client.get(f"/api/{player.sid}/stats?range=year")).status == 400
    assert (await client.get("/api/nope/stats")).status == 404


async def test_stats_route_is_cached(client, player, monkeypatch):
    calls = []

    def fake_summary(guild_id, range_name):
        calls.append((guild_id, range_name))
        return stats.empty_summary(range_name)

    monkeypatch.setattr(stats, "summary", fake_summary)
    for _ in range(3):
        assert (await client.get(f"/api/{player.sid}/stats")).status == 200
    await client.get(f"/api/{player.sid}/stats?range=all")
    assert calls == [(GUILD_ID, "week"), (GUILD_ID, "all")]


# --- Live viewers -----------------------------------------------------------------


async def viewers(client, player, viewer=None):
    headers = {"X-Viewer": viewer} if viewer else {}
    r = await client.get(f"/api/{player.sid}/state", headers=headers)
    return (await r.json())["viewers"]


async def test_viewer_count(client, player, fake_clock):
    assert await viewers(client, player) == 0
    assert await viewers(client, player, "tab-aaaaaaaa") == 1
    assert await viewers(client, player, "tab-aaaaaaaa") == 1
    assert await viewers(client, player, "tab-bbbbbbbb") == 2
    assert await viewers(client, player, "bad id!") == 2  # Ignored
    assert await viewers(client, player, "x" * 41) == 2
    fake_clock(web.VIEWER_WINDOW_SECONDS + 1)
    assert await viewers(client, player, "tab-bbbbbbbb") == 1  # A left


def test_viewer_map_is_pruned_and_capped(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(state, "clock", lambda: now[0])
    monkeypatch.setattr(web, "MAX_VIEWERS_PER_SESSION", 3)
    for i in range(5):
        web.count_viewer("s", f"viewer-{i:04d}")
    assert list(state.viewers["s"]) == [f"viewer-{i:04d}" for i in (2, 3, 4)]
    now[0] += web.VIEWER_WINDOW_SECONDS - 1
    web.count_viewer("s", "viewer-0003")  # Moves to the end
    now[0] += 2  # 2 and 4 are now too old
    assert web.count_viewer("s", None) == 1
    assert list(state.viewers["s"]) == ["viewer-0003"]


async def test_viewers_are_dropped_with_the_session(client, player, fake_clock):
    from djsnake.sessions import expire_session

    await viewers(client, player, "tab-aaaaaaaa")
    sid = player.sid
    assert sid in state.viewers
    expire_session(player)
    assert sid not in state.viewers
