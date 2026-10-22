"""Saved playlists: storage, the !playlist commands, permissions and the web
API. No network: downloads are replaced by a recorder."""

import sqlite3
import types

import pytest

from djsnake import commands, controls, playlists
from djsnake.config import MAX_SAVED_PLAYLISTS, MAX_SAVED_TRACKS
from djsnake.playback import play_next
from djsnake.playlists import PlaylistError
from fakes import make_ctx, make_track, settle

GUILD_ID = 1
ALICE, BOB, ADMIN = 101, 102, 103


def tr(n, **fields):
    track = {
        "url": f"https://www.youtube.com/watch?v=vid{n:08d}",
        "title": f"Track {n}",
        "duration": 60.0,
    }
    track.update(fields)
    return track


def code_of(fn, *args, **kwargs):
    with pytest.raises(PlaylistError) as e:
        fn(*args, **kwargs)
    return e.value.code


# --- Storage -------------------------------------------------------------------


def test_save_and_get_round_trip():
    result = playlists.save(GUILD_ID, "  Road   Trip ", [tr(1), tr(2)], ALICE)
    assert result == {"name": "Road Trip", "count": 2}
    got = playlists.get(GUILD_ID, "road trip")  # Case-insensitive
    assert got["name"] == "Road Trip"
    assert got["creator_id"] == ALICE
    assert [t["title"] for t in got["tracks"]] == ["Track 1", "Track 2"]
    assert got["tracks"][0]["url"].endswith("vid00000001")


def test_playlists_are_per_guild():
    playlists.save(1, "mix", [tr(1)])
    playlists.save(2, "mix", [tr(2)])
    assert playlists.get(2, "MIX")["tracks"][0]["title"] == "Track 2"
    assert code_of(playlists.get, 3, "mix") == "playlist_not_found"


def test_data_survives_a_restart():
    playlists.save(GUILD_ID, "keep", [tr(1)])
    # A new connection reads the same file: nothing is kept in memory.
    conn = sqlite3.connect(playlists.DB_PATH)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == playlists.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 1
    conn.close()
    assert playlists.list_all(GUILD_ID)[0]["name"] == "keep"


def test_list_all_counts_tracks_and_sums_duration():
    playlists.save(GUILD_ID, "b", [tr(1), tr(2)])
    playlists.save(GUILD_ID, "A", [tr(3)])
    assert playlists.list_all(GUILD_ID) == [
        {"name": "A", "tracks": 1, "duration": 60.0},
        {"name": "b", "tracks": 2, "duration": 120.0},
    ]


@pytest.mark.parametrize(
    "name", ["", "   ", "x" * 33, "@everyone", "`code`", "a\x07b", "<#123>", "-lead"]
)
def test_bad_names_are_refused(name):
    assert code_of(playlists.save, GUILD_ID, name, [tr(1)]) == "bad_playlist_name"


def test_nothing_to_save():
    assert code_of(playlists.save, GUILD_ID, "empty", []) == "playlist_empty"
    assert code_of(playlists.save, GUILD_ID, "x", [{"title": "no link"}]) == (
        "playlist_empty"
    )


def test_track_cap_per_playlist():
    full = [tr(i) for i in range(MAX_SAVED_TRACKS)]
    assert playlists.save(GUILD_ID, "full", full)["count"] == MAX_SAVED_TRACKS
    assert code_of(playlists.add_track, GUILD_ID, "full", tr(999)) == "playlist_full"
    assert code_of(playlists.save, GUILD_ID, "big", full + [tr(999)]) == (
        "playlist_full"
    )


def test_playlist_cap_per_guild():
    for i in range(MAX_SAVED_PLAYLISTS):
        playlists.save(GUILD_ID, f"p{i}", [tr(i)])
    assert code_of(playlists.save, GUILD_ID, "one more", [tr(1)]) == "playlist_limit"
    # Replacing an existing one is still fine, and other guilds are separate.
    playlists.save(GUILD_ID, "p0", [tr(1)], is_admin=True)
    playlists.save(2, "one more", [tr(1)])


def test_replace_needs_creator_or_manager():
    playlists.save(GUILD_ID, "mine", [tr(1)], creator_id=ALICE)
    assert code_of(playlists.save, GUILD_ID, "MINE", [tr(2)], BOB) == "playlist_exists"
    assert code_of(playlists.save, GUILD_ID, "mine", [tr(2)]) == "playlist_exists"
    playlists.save(GUILD_ID, "MINE", [tr(2), tr(3)], ALICE)
    assert len(playlists.get(GUILD_ID, "mine")["tracks"]) == 2
    playlists.save(GUILD_ID, "mine", [tr(4)], BOB, is_admin=True)
    got = playlists.get(GUILD_ID, "mine")
    assert [t["title"] for t in got["tracks"]] == ["Track 4"]
    assert got["creator_id"] == ALICE  # Replacing keeps the creator


def test_add_track_appends_in_order():
    playlists.save(GUILD_ID, "pl", [tr(1)])
    assert playlists.add_track(GUILD_ID, "PL", tr(2)) == {"name": "pl", "count": 2}
    titles = [t["title"] for t in playlists.get(GUILD_ID, "pl")["tracks"]]
    assert titles == ["Track 1", "Track 2"]
    assert code_of(playlists.add_track, GUILD_ID, "nope", tr(3)) == (
        "playlist_not_found"
    )


def test_delete_permissions_and_cascade():
    playlists.save(GUILD_ID, "pl", [tr(1), tr(2)], creator_id=ALICE)
    assert code_of(playlists.delete, GUILD_ID, "pl", BOB) == "playlist_forbidden"
    assert code_of(playlists.delete, GUILD_ID, "pl") == "playlist_forbidden"
    assert code_of(playlists.delete, GUILD_ID, "nope", ALICE) == "playlist_not_found"
    assert playlists.delete(GUILD_ID, "PL", ALICE) == "pl"
    conn = sqlite3.connect(playlists.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 0
    conn.close()
    playlists.save(GUILD_ID, "pl", [tr(1)], creator_id=ALICE)
    assert playlists.delete(GUILD_ID, "pl", BOB, is_admin=True) == "pl"


def test_unreadable_database_is_an_error_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(playlists, "DB_PATH", str(tmp_path))  # A directory
    with pytest.raises(controls.ControlError) as e:
        import asyncio

        asyncio.run(controls.control_playlist_list(GUILD_ID))
    assert e.value.code == "playlist_unavailable"
    assert e.value.status == 500


# --- Commands ------------------------------------------------------------------


def author(user_id, admin=False):
    return types.SimpleNamespace(
        id=user_id,
        display_name=f"user{user_id}",
        guild_permissions=types.SimpleNamespace(manage_guild=admin),
    )


def ctx_for(channel, user_id=ALICE, admin=False):
    return make_ctx(GUILD_ID, channel, author=author(user_id, admin), name="Guild")


@pytest.fixture
async def queued(player, ctx, media_dir):
    """A playing, B C queued."""
    for name in "ABC":
        player.add_to_queue(make_track(media_dir, name, duration=90.0))
    await play_next(ctx)
    return player


async def test_save_command_stores_current_track_and_queue(queued, channel):
    await commands.playlist_save.callback(ctx_for(channel), name="Evening")
    got = playlists.get(GUILD_ID, "evening")
    assert [t["title"] for t in got["tracks"]] == ["A", "B", "C"]
    assert got["creator_id"] == ALICE
    assert "❌" not in "".join(channel.log)


async def test_save_without_a_player_or_a_name(channel):
    await commands.playlist_save.callback(ctx_for(channel), name="x")
    await commands.playlist_save.callback(ctx_for(channel), name=None)
    assert "No music is currently playing" in channel.log[0]
    assert "Usage" in channel.log[1]


async def test_save_refuses_a_name_taken_by_someone_else(queued, channel):
    await commands.playlist_save.callback(ctx_for(channel, ALICE), name="mix")
    await commands.playlist_save.callback(ctx_for(channel, BOB), name="MIX")
    assert "Manage Server" in channel.log[-1]
    await commands.playlist_save.callback(ctx_for(channel, ADMIN, True), name="mix")
    assert len(channel.log) == 2  # No new error: the manager's save went through


async def test_add_command_appends_the_current_track(queued, channel):
    playlists.save(GUILD_ID, "pl", [tr(1)])
    await commands.playlist_add.callback(ctx_for(channel), name="PL")
    titles = [t["title"] for t in playlists.get(GUILD_ID, "pl")["tracks"]]
    assert titles == ["Track 1", "A"]
    await commands.playlist_add.callback(ctx_for(channel), name="missing")
    assert "No saved playlist" in channel.log[-1]


async def test_list_and_show_commands(channel):
    await commands.playlist_list.callback(ctx_for(channel))
    assert "No saved playlists" in channel.log[-1]
    playlists.save(GUILD_ID, "Chill", [tr(1), tr(2)])
    await commands.playlist_list.callback(ctx_for(channel))
    assert "**Chill** (2 tracks, 2:00)" in channel.log[-1]
    await commands.playlist_show.callback(ctx_for(channel), name="chill")
    assert "1. Track 1 (1:00)" in channel.log[-1]
    assert "2. Track 2 (1:00)" in channel.log[-1]
    await commands.playlist_show.callback(ctx_for(channel), name="nope")
    assert "No saved playlist" in channel.log[-1]


async def test_show_long_playlist_fits_in_one_message(channel):
    playlists.save(
        GUILD_ID, "long", [tr(i, title="T" * 150) for i in range(MAX_SAVED_TRACKS)]
    )
    await commands.playlist_show.callback(ctx_for(channel), name="long")
    assert len(channel.log[-1]) < 2000
    assert "more" in channel.log[-1]


async def test_delete_command_permissions(channel):
    playlists.save(GUILD_ID, "pl", [tr(1)], creator_id=ALICE)
    await commands.playlist_delete.callback(ctx_for(channel, BOB), name="pl")
    assert "Only the creator" in channel.log[-1]
    assert playlists.list_all(GUILD_ID)
    await commands.playlist_delete.callback(ctx_for(channel, ALICE), name="PL")
    assert not playlists.list_all(GUILD_ID)
    playlists.save(GUILD_ID, "pl", [tr(1)], creator_id=ALICE)
    await commands.playlist_delete.callback(ctx_for(channel, ADMIN, True), name="pl")
    assert not playlists.list_all(GUILD_ID)


async def test_load_command_queues_every_track(player, channel, monkeypatch):
    playlists.save(GUILD_ID, "pl", [tr(1), tr(2), {"url": "https://evil.test/x"}])
    seen = []

    async def fake_queue(ctx, p, entries, **kwargs):
        seen.append(entries)

    async def fake_join(ctx):
        return True

    monkeypatch.setattr(commands, "queue_playlist", fake_queue)
    monkeypatch.setattr(commands, "join_voice_channel", fake_join)
    await commands.playlist_load.callback(ctx_for(channel), name="PL")
    # The foreign link was dropped on the way out
    assert [e["title"] for e in seen[0]] == ["Track 1", "Track 2"]
    assert seen[0][0]["url"] == "https://www.youtube.com/watch?v=vid00000001"
    await commands.playlist_load.callback(ctx_for(channel), name="none")
    assert "No saved playlist" in channel.log[-1]
    assert len(seen) == 1


# --- Web API -------------------------------------------------------------------


async def test_api_lists_playlists(client, player):
    playlists.save(GUILD_ID, "Chill", [tr(1), tr(2)], creator_id=ALICE)
    r = await client.get(f"/api/{player.sid}/playlists")
    assert r.status == 200
    assert await r.json() == {
        "playlists": [{"name": "Chill", "tracks": 2, "duration": 120.0}]
    }
    r = await client.get("/api/nosuchsid/playlists")
    assert r.status == 404


async def test_api_save_playlist(client, player, queued):
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "save_playlist", "name": "Web"}
    )
    assert (r.status, await r.json()) == (200, {"ok": True})
    got = playlists.get(GUILD_ID, "web")
    assert [t["title"] for t in got["tracks"]] == ["A", "B", "C"]
    assert got["creator_id"] is None
    # The page cannot replace a playlist that already exists
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "save_playlist", "name": "WEB"}
    )
    assert r.status == 409
    assert "already exists" in (await r.json())["error"]


async def test_api_save_validation(client, player):
    for body in (
        {"action": "save_playlist"},
        {"action": "save_playlist", "name": 5},
        {"action": "load_playlist", "name": ""},
    ):
        r = await client.post(f"/api/{player.sid}/control", json=body)
        assert r.status == 400
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "save_playlist", "name": "@all"}
    )
    assert r.status == 400
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "save_playlist", "name": "ok"}
    )
    assert r.status == 400  # Nothing queued: nothing to save
    assert "nothing to save" in (await r.json())["error"]


async def test_api_load_playlist(client, player, monkeypatch):
    playlists.save(GUILD_ID, "pl", [tr(1), tr(2)])
    seen = []

    async def fake_queue(ctx, p, entries, **kwargs):
        seen.append((entries, kwargs))

    monkeypatch.setattr(controls, "queue_playlist", fake_queue)
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "load_playlist", "name": "PL"}
    )
    await settle()
    assert (r.status, await r.json()) == (202, {"ok": True})
    assert [e["title"] for e in seen[0][0]] == ["Track 1", "Track 2"]
    assert seen[0][1] == {"from_web": True}

    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "load_playlist", "name": "no"}
    )
    assert r.status == 404


async def test_api_load_needs_the_bot_in_voice(client, player):
    playlists.save(GUILD_ID, "pl", [tr(1)])
    player.voice_client.connected = False
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "load_playlist", "name": "pl"}
    )
    assert r.status == 409
