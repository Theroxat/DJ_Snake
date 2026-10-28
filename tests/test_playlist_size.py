"""Long playlists: over MAX_PLAYLIST_SIZE the requester picks the first
MAX_PLAYLIST_SIZE tracks or the whole list (up to MAX_PLAYLIST_FULL and the
room left in the queue). Discord asks with buttons, the clip page with a
dialog fed by a 409 `playlist_choice` answer."""

import asyncio
import types

import pytest

from djsnake import catalog, commands, config, controls, limits, media, spotify
from djsnake.controls import size_choice, sized
from djsnake.player import MusicPlayer

ID = "37i9dQZF1DXcBWIGoYBM5M"


def flat(n):
    return [{"id": f"v{i:010d}", "title": f"T{i}"} for i in range(n)]


def meta(n):
    return [{"title": f"Song {i}", "artists": ["A"], "duration": 200} for i in range(n)]


# --- size_choice() and sized() --------------------------------------------------


def test_no_choice_up_to_the_default_size():
    assert size_choice(flat(30), room=200) is None
    assert sized(flat(20), None, None) == flat(20)


def test_choice_above_the_default_size():
    entries = flat(50)
    choice = size_choice(entries, room=200)
    assert choice == {
        "count": 50,
        "more": False,
        "default": 30,
        "max": 50,
        "whole": True,
        "truncated": False,
    }
    assert len(sized(entries, "default", choice)) == 30
    assert sized(entries, "full", choice) == entries


def test_full_is_capped_at_max_playlist_full():
    entries = flat(201)  # Readers fetch one more than MAX_PLAYLIST_FULL
    choice = size_choice(entries, room=500)
    assert choice["count"] == 200 and choice["more"] and not choice["whole"]
    assert len(sized(entries, "full", choice)) == 200


def test_full_never_exceeds_the_room_in_the_queue():
    choice = size_choice(flat(120), room=45)
    assert choice["max"] == 45 and not choice["whole"]
    assert len(sized(flat(120), "full", choice)) == 45
    # No more room than the default: nothing to choose, the default applies.
    assert size_choice(flat(120), room=30) is None
    assert len(sized(flat(120), "full", None)) == 30


def test_room_counts_the_queue_and_the_per_user_limit(monkeypatch):
    player = MusicPlayer()
    player.queue = [{"requester_id": 7}] * 10 + [{"requester_id": 8}] * 5
    monkeypatch.setattr(config, "MAX_QUEUE_SIZE", 100)
    assert limits.room(player, 1, 7) == 85
    monkeypatch.setattr(config, "MAX_TRACKS_PER_USER", 40)
    assert limits.room(player, 1, 7) == 30
    assert limits.room(player, 1, 7, bypass=True) == 85
    monkeypatch.setattr(config, "MAX_QUEUE_SIZE", 12)
    assert limits.room(player, 1, 8) == 0


def test_truncated_track_lists_are_flagged():
    tracks = spotify.TrackList(meta(100))
    tracks.truncated = True
    assert size_choice(tracks, room=200)["truncated"] is True


# --- Readers fetch up to MAX_PLAYLIST_FULL + 1 ------------------------------------


def test_flat_extraction_reads_one_more_than_the_full_cap(monkeypatch):
    seen = {}

    class FakeYDL:
        def __init__(self, opts):
            seen.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return {"_type": "playlist", "entries": flat(3)}

    monkeypatch.setattr(media.yt_dlp, "YoutubeDL", FakeYDL)
    assert (
        len(media.get_playlist_info("https://www.youtube.com/playlist?list=PLx")) == 3
    )
    assert seen["playlistend"] == config.MAX_PLAYLIST_FULL + 1


async def test_read_spotify_asks_for_one_more_than_the_full_cap(monkeypatch):
    limits_seen = []

    def fake(provider, kind, ref, limit):
        limits_seen.append(limit)
        return meta(3)

    monkeypatch.setattr(catalog, "get_tracks", fake)
    await controls.read_spotify("playlist", "1", "deezer")
    assert limits_seen == [config.MAX_PLAYLIST_FULL + 1]


@pytest.mark.parametrize(
    "listed, limit, truncated", [(100, 201, True), (99, 201, False), (100, 31, False)]
)
def test_spotify_embed_cap_is_detected(monkeypatch, listed, limit, truncated):
    monkeypatch.setattr(spotify, "api_configured", lambda: False)
    monkeypatch.setattr(spotify, "fetch_embed_tracks", lambda kind, i: meta(listed))
    tracks = spotify.get_tracks("playlist", ID, limit)
    assert tracks.truncated is truncated
    assert len(tracks) == min(listed, limit)


def test_spotify_web_api_is_not_flagged(monkeypatch):
    monkeypatch.setattr(spotify, "api_configured", lambda: True)
    monkeypatch.setattr(spotify, "fetch_api_tracks", lambda k, i, limit: meta(150))
    tracks = spotify.get_tracks("playlist", ID, 201)
    assert len(tracks) == 150 and not tracks.truncated


def test_deezer_asks_for_the_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        spotify, "_http", lambda url, **k: calls.append(url) or (200, b'{"data": []}')
    )
    with pytest.raises(spotify.SpotifyError):
        catalog.get_tracks("deezer", "playlist", "5", 201)
    assert calls == ["https://api.deezer.com/playlist/5/tracks?limit=201"]


# --- The Discord prompt ----------------------------------------------------------


class FakeResponse:
    def __init__(self):
        self.sent, self.deferred = [], False

    async def send_message(self, content=None, ephemeral=False, **kwargs):
        self.sent.append((content, ephemeral))

    async def defer(self, **kwargs):
        self.deferred = True


def interaction(user_id):
    return types.SimpleNamespace(
        user=types.SimpleNamespace(id=user_id), response=FakeResponse()
    )


def labels(view):
    return [item.label for item in view.children]


async def test_mix_link_gets_one_prompt_with_three_buttons():
    entries = flat(80)
    view = commands.PlaylistChoiceView(
        1, single=True, entries=entries, choice=size_choice(entries, 200)
    )
    assert labels(view) == ["This track only", "First 30", "Whole playlist (80)"]


async def test_mix_link_within_the_default_keeps_two_buttons():
    view = commands.PlaylistChoiceView(1, single=True, entries=flat(12))
    assert labels(view) == ["This track only", "Whole playlist"]


async def test_size_prompt_offers_first_n_beyond_the_full_cap():
    entries = flat(201)
    view = commands.PlaylistChoiceView(
        1, single=False, entries=entries, choice=size_choice(entries, 500)
    )
    assert labels(view) == ["First 30", "First 200"]


async def test_only_the_requester_may_press():
    entries = flat(50)
    view = commands.PlaylistChoiceView(
        42, single=False, entries=entries, choice=size_choice(entries, 200)
    )
    other = interaction(7)
    assert await view.interaction_check(other) is False
    assert other.response.sent == [("❌ This isn't your command.", True)]
    owner = interaction(42)
    assert await view.interaction_check(owner) is True
    await view.children[1].callback(owner)
    assert view.choice == "full" and owner.response.deferred
    assert view.is_finished()


@pytest.mark.parametrize(
    "single, picked, text",
    [
        (False, "default", "⏱️ No answer: loading the first 30 tracks."),
        (True, "single", "⏱️ No answer: playing this track only."),
    ],
)
async def test_timeout_picks_the_default_and_says_so(
    ctx, channel, monkeypatch, single, picked, text
):
    monkeypatch.setattr(commands, "PLAYLIST_CHOICE_SECONDS", 0.05)
    entries = flat(60)
    got = await commands.ask_playlist_size(
        ctx, entries, size_choice(entries, 200), single=single
    )
    assert got == picked
    assert "**60**" in channel.log[-1]
    assert channel.edits == [text]


def test_prompt_text_mentions_the_room_and_the_spotify_cap():
    tracks = spotify.TrackList(meta(100))
    tracks.truncated = True
    text = commands._choice_text("en", tracks, size_choice(tracks, 60), False)
    assert "**100**" in text
    assert "room for 60 more" in text
    assert "only the first 100 tracks without API credentials" in text
    more = commands._choice_text("en", flat(201), size_choice(flat(201), 900), True)
    assert "**200+**" in more


@pytest.fixture
def play_env(ctx, monkeypatch):
    """!play with voice, the prompt and queue_playlist faked."""
    asked, queued = [], []
    env = types.SimpleNamespace(asked=asked, queued=queued, answer="full")

    async def join(c):
        return True

    async def ask(c, entries, choice, single=False):
        asked.append((len(entries), choice, single))
        return env.answer

    async def fake_queue(c, player, entries, from_web=False):
        queued.append(len(entries))

    async def fake_download(c, player, url, **kwargs):
        queued.append(url)

    monkeypatch.setattr(commands, "join_voice_channel", join)
    monkeypatch.setattr(commands, "ask_playlist_size", ask)
    monkeypatch.setattr(commands, "queue_playlist", fake_queue)
    monkeypatch.setattr(commands, "download_and_queue_song", fake_download)
    state_player = MusicPlayer()
    state_player.voice_client = types.SimpleNamespace(is_connected=lambda: True)
    from djsnake import state

    state.music_players[ctx.guild.id] = state_player
    env.player = state_player
    return env


PLAYLIST = "https://www.youtube.com/playlist?list=PLabc"
MIX = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ"


async def test_play_short_playlist_has_no_prompt(ctx, play_env, monkeypatch):
    monkeypatch.setattr(media, "get_playlist_info", lambda url: flat(25))
    await commands.play.callback(ctx, query=PLAYLIST)
    assert play_env.asked == [] and play_env.queued == [25]


@pytest.mark.parametrize("answer, loaded", [("full", 75), ("default", 30)])
async def test_play_long_playlist_asks(ctx, play_env, monkeypatch, answer, loaded):
    play_env.answer = answer
    monkeypatch.setattr(media, "get_playlist_info", lambda url: flat(75))
    await commands.play.callback(ctx, query=PLAYLIST)
    ((count, choice, single),) = play_env.asked
    assert (count, choice["max"], single) == (75, 75, False)
    assert play_env.queued == [loaded]


async def test_play_full_respects_the_queue_size(ctx, play_env, monkeypatch):
    monkeypatch.setattr(config, "MAX_QUEUE_SIZE", 50)
    play_env.player.queue = [{"requester_id": None}] * 10
    monkeypatch.setattr(media, "get_playlist_info", lambda url: flat(201))
    await commands.play.callback(ctx, query=PLAYLIST)
    assert play_env.asked[0][1]["max"] == 40
    assert play_env.queued == [40]


async def test_play_mix_asks_once_with_the_size(ctx, play_env, monkeypatch):
    play_env.answer = "single"
    monkeypatch.setattr(media, "get_playlist_info", lambda url: flat(50))
    await commands.play.callback(ctx, query=MIX)
    ((count, choice, single),) = play_env.asked
    assert single and choice["max"] == 50
    assert play_env.queued == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]


async def test_play_spotify_playlist_asks_and_slices(ctx, play_env, monkeypatch):
    got = []

    async def fake_read(kind, ref, provider="spotify", limit=None):
        return meta(120)

    async def fake_queue_spotify(c, player, kind, tracks, from_web=False):
        got.append((kind, len(tracks)))

    monkeypatch.setattr(commands, "read_spotify", fake_read)
    monkeypatch.setattr(commands, "queue_spotify", fake_queue_spotify)
    await commands.play.callback(ctx, query=f"https://open.spotify.com/playlist/{ID}")
    assert play_env.asked[0][1]["count"] == 120
    assert got == [("playlist", 120)]


# --- The clip page: 409 playlist_choice, then add with `size` --------------------


@pytest.fixture
def web_env(client, player, monkeypatch):
    queued = []

    async def fake_queue(c, p, entries, from_web=False):
        queued.append(len(entries))

    async def fake_queue_spotify(c, p, kind, tracks, from_web=False):
        queued.append(len(tracks))

    monkeypatch.setattr(controls, "queue_playlist", fake_queue)
    monkeypatch.setattr(controls, "queue_spotify", fake_queue_spotify)

    async def add(url, **extra):
        r = await client.post(
            f"/api/{player.sid}/control", json={"action": "add", "url": url, **extra}
        )
        body = await r.json()
        await asyncio.sleep(0.05)
        return r.status, body

    return types.SimpleNamespace(add=add, queued=queued)


async def test_web_long_playlist_round_trip(web_env, monkeypatch):
    monkeypatch.setattr(media, "get_playlist_info", lambda url: flat(90))
    status, body = await web_env.add(PLAYLIST)
    assert status == 409 and body["ok"] is False
    assert body["choice"] == {
        "count": 90,
        "more": False,
        "default": 30,
        "max": 90,
        "whole": True,
        "truncated": False,
    }
    assert body["error"] == "This playlist has 90 tracks: choose how many to load."
    assert web_env.queued == []
    assert await web_env.add(PLAYLIST, size="full") == (202, {"ok": True})
    assert await web_env.add(PLAYLIST, size="default") == (202, {"ok": True})
    assert web_env.queued == [90, 30]


async def test_web_short_playlist_needs_no_choice(web_env, monkeypatch):
    monkeypatch.setattr(media, "get_playlist_info", lambda url: flat(10))
    assert await web_env.add(PLAYLIST) == (202, {"ok": True})
    assert web_env.queued == [10]


async def test_web_spotify_choice(web_env, monkeypatch):
    def fake(provider, kind, ref, limit):
        tracks = spotify.TrackList(meta(100))
        tracks.truncated = True
        return tracks

    monkeypatch.setattr(catalog, "get_tracks", fake)
    url = f"https://open.spotify.com/playlist/{ID}"
    status, body = await web_env.add(url)
    assert status == 409 and body["choice"]["truncated"] is True
    assert await web_env.add(url, size="full") == (202, {"ok": True})
    assert web_env.queued == [100]


@pytest.mark.parametrize("size", ["all", 200, True, ""])
async def test_web_bad_size_is_refused(web_env, size):
    status, body = await web_env.add(PLAYLIST, size=size)
    assert status == 400 and body == {"ok": False, "error": "Invalid playlist size."}
