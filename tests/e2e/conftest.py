"""Fixtures of the end-to-end tests (marked `e2e`, run with `pytest -m e2e`):
real FFmpeg on media generated at run time, a voice client that pulls the
real PCM frames, and (test_e2e_web.py) the real web app in Chromium."""

import shutil

import pytest

from djsnake import state
from djsnake.player import MusicPlayer
from djsnake.sessions import open_session
from e2e_kit import GUILD_ID, MediaKit, PacedVoiceClient, make_media
from fakes import FakeChannel, make_ctx


@pytest.fixture(scope="session")
def media_kit(tmp_path_factory):
    """chirp.mp4 and tone.mp4, generated and analysed once per run."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    return MediaKit(make_media(str(tmp_path_factory.mktemp("e2e-media"))))


@pytest.fixture
def e2e_ctx():
    return make_ctx(GUILD_ID, FakeChannel(), name="E2E Guild")


@pytest.fixture
async def live_player(e2e_ctx):
    """Factory: a player in voice with a live session and a PacedVoiceClient
    reading at `speed` times real time. Stopped and joined at teardown."""
    made = []

    async def make(speed=1.0):
        player = MusicPlayer()
        state.music_players[GUILD_ID] = player
        player.voice_client = PacedVoiceClient(speed)
        await open_session(e2e_ctx, player, announce=True)
        made.append(player)
        return player

    yield make
    for player in made:
        player.should_stop = True  # The last `after` callback starts nothing
        player.voice_client.shutdown()
