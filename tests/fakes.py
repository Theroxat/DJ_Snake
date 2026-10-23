"""Stand-ins for Discord objects and FFmpeg, shared by the tests."""

import array
import asyncio
import os
import re
import secrets
import threading
import types

import discord

from djsnake.config import FRAMES_PER_SECOND, PCM_FRAME_SIZE


async def settle(seconds=0.15):
    """Let callbacks scheduled from the fake audio thread run."""
    await asyncio.sleep(seconds)


class FakeMessage:
    def __init__(self, channel, content):
        self.channel, self.content, self.deleted = channel, content, False
        self.fail_with = None  # Exception raised by the next edit()

    async def edit(self, content=None, **kwargs):
        if self.fail_with:
            raise self.fail_with
        self.content = content
        self.channel.edits.append(content)

    async def delete(self):
        self.deleted = True
        self.channel.deletes.append(self)

    async def add_reaction(self, emoji):
        self.channel.reactions.append(emoji)


class FakeChannel:
    def __init__(self):
        self.log = []  # Contents of sent messages
        self.edits = []
        self.deletes = []
        self.reactions = []
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.log.append(str(content))
        message = FakeMessage(self, content)
        self.messages.append(message)
        return message


class FakeAuthor:
    def __init__(self, dms_open=True):
        self.dms_open, self.dms = dms_open, []

    async def send(self, content=None, **kwargs):
        if not self.dms_open:
            raise discord.Forbidden(
                types.SimpleNamespace(status=403, reason="Forbidden"), "closed"
            )
        self.dms.append(content)


def make_ctx(guild_id, channel, author=None, command=None, name=None):
    """A command context: guild, channel, author and the command message."""
    return types.SimpleNamespace(
        guild=types.SimpleNamespace(id=guild_id, name=name or f"Guild{guild_id}"),
        channel=channel,
        author=author,
        send=channel.send,
        message=FakeMessage(channel, "!cmd"),
        command=command,
    )


class FakeInteraction:
    """The slice of discord.Interaction a slash command context uses."""

    def __init__(self, channel):
        self.channel, self.done = channel, False
        self.deferred = []  # `ephemeral` of each defer()
        self.edits = []  # edit_original_response(content=...)
        self.response = types.SimpleNamespace(is_done=lambda: self.done)

    async def edit_original_response(self, content=None, **kwargs):
        self.edits.append(content)


def make_slash_ctx(guild_id, channel, author=None, command=None, name=None):
    """A hybrid command context invoked as `/command`: an interaction, a
    synthetic message that cannot take reactions, and replies recorded in
    `ctx.replies` as (content, ephemeral)."""
    interaction = FakeInteraction(channel)
    ctx = make_ctx(guild_id, channel, author, command, name)
    ctx.interaction = interaction
    ctx.message = None
    ctx.replies = []

    async def send(content=None, ephemeral=False, **kwargs):
        interaction.done = True
        ctx.replies.append((content, ephemeral))
        return await channel.send(content, **kwargs)

    async def defer(ephemeral=False):
        interaction.done = True
        interaction.deferred.append(ephemeral)

    ctx.send, ctx.defer = send, defer
    return ctx


class FakeVoiceClient:
    """Plays nothing; stop() calls `after` from another thread, like
    discord.py's audio thread does."""

    def __init__(self):
        self.connected = True
        self.playing = False
        self.paused = False
        self.after = None
        self.source = None
        self.played = []  # Basenames of the files started with play()
        self.channel = types.SimpleNamespace(name="General")

    def play(self, source, after):
        if self.playing:
            raise RuntimeError("Already playing audio.")
        self.playing, self.paused, self.after = True, False, after
        self.source = source
        inner = getattr(source, "source", source)  # Unwrap the CrossfadeMixer
        self.played.append(os.path.basename(inner.path))

    def is_playing(self):
        return self.playing and not self.paused

    def is_paused(self):
        return self.playing and self.paused

    def is_connected(self):
        return self.connected

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        if self.playing:
            self.playing = self.paused = False
            threading.Thread(target=self.after, args=(None,)).start()

    def finish(self):
        """The source ran out: discord.py calls `after` itself."""
        self.playing = False
        self.after(None)

    async def disconnect(self, force=False):
        self.stop()
        self.connected = False


class FakeSource:
    """FFmpegPCMAudio stand-in: constant-value s16 frames for the `-t` length
    (practically endless without it)."""

    def __init__(self, path, value=1000, **kwargs):
        self.path, self.kwargs, self.cleaned = path, kwargs, False
        match = re.search(r"-t ([0-9.]+)", kwargs.get("before_options", ""))
        seconds = float(match.group(1)) if match else None
        self.left = round(seconds * FRAMES_PER_SECOND) if seconds else 10**9
        self.frame = array.array("h", [value] * (PCM_FRAME_SIZE // 2)).tobytes()

    def read(self):
        if self.cleaned or self.left <= 0:
            return b""
        self.left -= 1
        return self.frame

    def cleanup(self):
        self.cleaned = True


class CountedSource:
    """A source with a fixed number of silent frames."""

    def __init__(self, frames):
        self.frames, self.cleaned = frames, False

    def read(self):
        if self.frames:
            self.frames -= 1
            return b"\0" * PCM_FRAME_SIZE
        return b""

    def cleanup(self):
        self.cleaned = True


def sample(frame):
    """The first sample of a PCM frame."""
    values = array.array("h")
    values.frombytes(frame)
    return values[0]


def make_track(directory, name, **fields):
    """A queued-track dict backed by a small file in `directory`."""
    path = os.path.join(directory, name + ".mp4")
    with open(path, "wb") as f:
        f.write(b"x" * 64)
    track = {
        "id": secrets.token_hex(4),
        "filepath": path,
        "title": name,
        "duration": 100.0,
        "thumbnail": None,
        "url": f"https://www.youtube.com/watch?v={name}",
    }
    track.update(fields)
    return track


def titles(tracks):
    return [t["title"] for t in tracks]
