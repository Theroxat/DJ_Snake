"""Shared fixtures. No network, no Discord: voice, channels and FFmpeg are
faked (see fakes.py)."""

import os
import shutil
import tempfile

# Configuration is read at import, so set it before djsnake is imported.
_ROOT = tempfile.mkdtemp(prefix="djsnake-tests-")
os.environ.update(
    {
        "TEMP_DIR": os.path.join(_ROOT, "media"),
        "DATA_DIR": os.path.join(_ROOT, "data"),
        "WEB_THEME": "minimal",
        "COMMAND_PREFIX": "!",
        "BOT_LANGUAGE": "en",
        "SESSION_IDLE_HOURS": "6",
    }
)

import discord  # noqa: E402
import pytest  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from djsnake import (  # noqa: E402
    i18n,
    limits,
    lyrics,
    media,
    permissions,
    playlists,
    state,
    stay,
    stats,
    themes,
    volume,
    voteskip,
    web,
)
from djsnake.player import MusicPlayer  # noqa: E402
from djsnake.sessions import open_session  # noqa: E402
from djsnake.web import build_web_app  # noqa: E402
from fakes import FakeChannel, FakeSource, FakeVoiceClient, make_ctx  # noqa: E402

GUILD_ID = 1


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    """Empty shared state, and data/media directories private to the test."""
    for registry in (
        state.music_players,
        state.sessions,
        state.watch_ids,
        state.guild_perms,
        state.guild_themes,
        state.guild_volumes,
        state.guild_voteskip,
        state.guild_limits,
        state.play_times,
        state.guild_stay,
        state.stay_retries,
        state.guild_languages,
        state.edit_tokens,
        state.viewers,
        state.streams,
        stats._peaks,
        web._stats_cache,
        lyrics._cache,
        lyrics._tasks,
    ):
        registry.clear()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(stats, "DB_PATH", str(data_dir / "stats.db"))
    monkeypatch.setattr(playlists, "DB_PATH", str(data_dir / "playlists.db"))
    monkeypatch.setattr(themes, "THEMES_FILE", str(data_dir / "themes.json"))
    monkeypatch.setattr(themes, "LOGO_DIR", str(data_dir / "logos"))
    monkeypatch.setattr(volume, "VOLUME_FILE", str(data_dir / "volume.json"))
    monkeypatch.setattr(voteskip, "VOTESKIP_FILE", str(data_dir / "voteskip.json"))
    monkeypatch.setattr(limits.config, "LIMITS_FILE", str(data_dir / "limits.json"))
    monkeypatch.setattr(stay, "STAY_FILE", str(data_dir / "stay.json"))
    monkeypatch.setattr(stay, "STAY_247", False)
    monkeypatch.setattr(lyrics, "LYRICS_DIR", str(data_dir / "lyrics"))
    monkeypatch.setattr(lyrics, "ENABLED", True)

    def no_network(url):
        raise lyrics.LyricsUnavailable("no network in tests")

    monkeypatch.setattr(lyrics, "_http", no_network)
    monkeypatch.setattr(
        permissions, "PERMISSIONS_FILE", str(data_dir / "permissions.json")
    )
    monkeypatch.setattr(i18n, "LANGUAGES_FILE", str(data_dir / "languages.json"))
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    monkeypatch.setattr(media, "TEMP_DIR", str(media_dir))
    yield
    for player in state.music_players.values():
        try:
            player.cancel_idle()
        except RuntimeError:
            pass  # Its loop is already closed
        if player.panel_task is not None and not player.panel_task.done():
            try:
                player.panel_task.cancel()
            except RuntimeError:
                pass  # Its loop is already closed
    stats.flush()  # Records of this test go to its own database
    state.counters["tracks_played"] = 0
    state.music_players.clear()
    state.sessions.clear()
    state.watch_ids.clear()


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def media_dir(tmp_path):
    return str(tmp_path / "media")


class FFmpegRecorder:
    """Replaces discord.FFmpegPCMAudio: records the options of every call and
    returns a FakeSource whose sample value is picked per file prefix."""

    def __init__(self):
        self.calls = []  # kwargs of each call
        self.made = []  # FakeSource instances
        self.values = {}  # file name prefix (2 chars) -> sample value

    def __call__(self, path, **kwargs):
        self.calls.append(kwargs)
        value = self.values.get(os.path.basename(path)[:2], 1000)
        source = FakeSource(path, value, **kwargs)
        self.made.append(source)
        return source

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture
def ffmpeg(monkeypatch):
    recorder = FFmpegRecorder()
    monkeypatch.setattr(discord, "FFmpegPCMAudio", recorder)
    return recorder


@pytest.fixture
def channel():
    return FakeChannel()


@pytest.fixture
def ctx(channel):
    return make_ctx(GUILD_ID, channel, name="Guild")


@pytest.fixture
async def player(ctx, ffmpeg):
    """A player in voice with a live web session (link posted in `channel`)."""
    p = MusicPlayer()
    state.music_players[GUILD_ID] = p
    p.voice_client = FakeVoiceClient()
    await open_session(ctx, p, announce=True)
    return p


@pytest.fixture
async def client():
    test_client = TestClient(TestServer(build_web_app()))
    await test_client.start_server()
    yield test_client
    await test_client.close()


@pytest.fixture
def fake_clock(monkeypatch):
    """Call with an offset in seconds to move state.clock() forward."""
    real = state.clock

    def advance(seconds):
        monkeypatch.setattr(state, "clock", lambda: real() + seconds)

    return advance
