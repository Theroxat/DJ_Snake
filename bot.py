import os
import asyncio
import audioop  # Standard library up to 3.12; audioop-lts on 3.13+
import json
import math
import re
import secrets
import subprocess
import threading
import time
import types
import yt_dlp
import discord
from discord.ext import commands
from yt_dlp.postprocessor import ModifyChaptersPP, SponsorBlockPP
import tempfile
from aiohttp import web
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure intents with all necessary permissions
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.guild_messages = True

# Configuration from environment variables
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
MAX_PLAYLIST_SIZE = int(os.getenv("MAX_PLAYLIST_SIZE", "30"))
# Tracks are downloaded as one video+audio mp4 (max 720p, H.264 so every
# browser can play it): Discord gets the audio, the web page shows the clip.
DEFAULT_MEDIA_FORMAT = (
    "bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]"
    "/best[height<=720][ext=mp4]/best[height<=720]/best"
)
MEDIA_FORMAT = os.getenv("MEDIA_FORMAT") or DEFAULT_MEDIA_FORMAT
if os.getenv("AUDIO_FORMAT") and not os.getenv("MEDIA_FORMAT"):
    print("⚠️  AUDIO_FORMAT is no longer read; set MEDIA_FORMAT instead.")

# Web server for the synced clip page. The public URL is what gets posted in
# Discord; the tunnel in front of it forwards to WEB_PORT.
WEB_PORT = int(os.getenv("WEB_PORT", "47823"))
# Empty PUBLIC_BASE_URL falls back to the local address.
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL") or f"http://localhost:{WEB_PORT}"
).rstrip("/")
WEB_INDEX = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "web", "index.html"
)
# A web session outlives the bot's stay in voice; it expires after this many
# hours without activity (playback, a command or a web control).
SESSION_IDLE_HOURS = float(os.getenv("SESSION_IDLE_HOURS", "6"))
SESSION_CLEANUP_SECONDS = 600
# Persistent data (survives restarts): per-guild web page themes and logos.
DATA_DIR = os.getenv("DATA_DIR") or "/app/data"
THEMES_FILE = os.path.join(DATA_DIR, "themes.json")
LOGO_DIR = os.path.join(DATA_DIR, "logos")
# Clip page themes. "perso" is a custom palette on top of a preset's shapes.
THEME_PRESETS = ("arcade", "minimal", "nuit", "vinyle", "club", "terminal")
THEME_SKINS = THEME_PRESETS + ("perso",)
# Fallback theme for guilds that never chose one.
WEB_THEME = (os.getenv("WEB_THEME") or "arcade").strip().lower()
if WEB_THEME not in THEME_PRESETS:
    print(f"⚠️  WEB_THEME={WEB_THEME!r} is not a preset; using 'arcade'.")
    WEB_THEME = "arcade"
PERSO_DEFAULTS = {
    "base": "nuit",
    "bg": "#101820",
    "text": "#f2f2f2",
    "accent": "#ffb703",
}
THEME_NAME_MAX = 32
THEME_EDIT_SECONDS = 30 * 60  # Lifetime of a `!theme edit` link
MAX_LOGO_BYTES = 512 * 1024
LOGO_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}
# Played tracks kept (files on disk) so "previous" can go back.
HISTORY_SIZE = 5
# "Previous" restarts the current track once it has played this long.
PREVIOUS_RESTART_SECONDS = 5
MAX_CONTROL_BODY = 4096
# YouTube signs stream URLs with a JS challenge. yt-dlp needs both a JS
# runtime (deno, installed in the image) and the EJS solver script, which it
# fetches on first use. Without it, signature/n solving fails and some
# formats are unavailable.
REMOTE_COMPONENTS = ["ejs:github"]

# YouTube challenges requests coming from datacenter IPs with "Sign in to
# confirm you're not a bot". Answering it requires a Proof-of-Origin token;
# without a provider yt-dlp reports `PO Token Providers: none` and the download
# fails. The provider runs as the `pot-provider` service in docker-compose.
#
# The extractor arg key matters: `youtube:getpot_bgutil_baseurl` is deprecated
# and rejected by the plugin, which then silently falls back to its default.
POT_BASE_URL = os.getenv("POT_BASE_URL", "http://pot-provider:4416")
EXTRACTOR_ARGS = {"youtubepot-bgutilhttp": {"base_url": [POT_BASE_URL]}}

# A challenge that still slips through should cost a few seconds of retry, not
# the whole track. One entry in the backoff tuple per retry.
DOWNLOAD_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (3, 8)

# Retrying these is pointless: the video will not become available.
PERMANENT_ERROR_MARKERS = (
    "video unavailable",
    "private video",
    "removed by the uploader",
    "members-only",
    "who has blocked it",
    "copyright",
)
# Automix, always on. At download, SponsorBlock "music_offtopic" segments
# (skits, talking intros) are cut out of the file. One ffmpeg pass then
# measures loudness (loudnorm, first pass) and leading/trailing silence.
# Playback normalises with loudnorm's second pass, skips that silence and
# fades in and out.
SPONSORBLOCK_REMOVE = ["music_offtopic"]
# Linear mode (a plain gain) needs the measured range within LRA and the
# boosted true peak under TP; loudnorm otherwise falls back to its dynamic mode.
LOUDNORM_TARGET = "I=-14:TP=-1.5:LRA=11"
SILENCE_FILTER = "silencedetect=noise=-45dB:d=0.5"
MAX_LEADING_TRIM = 20.0  # Never skip more than this at the start
MIN_PLAYED_SECONDS = 10.0  # Trimming that leaves less is ignored
FADE_IN_SECONDS = 1.0
SEEK_FADE_IN_SECONDS = 0.3  # Softens the cut when restarting mid-track
FADE_OUT_SECONDS = 4.0
ANALYSIS_TIMEOUT = 180
LOUDNORM_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
# Optional crossfade (per session, off by default): the last seconds of a track
# overlap the first ones of the next. Both need an automix window this long.
CROSSFADE_SECONDS = 6.0
CROSSFADE_MIN_TRACK = 15.0
FRAMES_PER_SECOND = 50  # discord.py reads 20 ms PCM frames
PCM_FRAME_SIZE = discord.opus.Encoder.FRAME_SIZE  # 3840 bytes, 48 kHz s16 stereo

# Only create a temp directory if one wasn't provided. Using tempfile.mkdtemp()
# directly as the default argument would create an orphan directory on every run
# because default arguments are evaluated eagerly.
TEMP_DIR = os.getenv("TEMP_DIR") or tempfile.mkdtemp()

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
temp_dir = TEMP_DIR


def _clock():
    """Wall clock for session expiry (a function so tests can fake it)."""
    return time.time()


class MusicPlayer:
    def __init__(self):
        self.voice_client = None
        self.queue = []
        self.history = []  # Last played tracks, oldest first (files kept)
        self.is_playing = False  # A track is loaded in the voice client
        self.should_stop = False
        # Bumped by !stop / !reset: download jobs started before it give up,
        # even if a later !play clears should_stop again.
        self.epoch = 0
        self.now_playing = None
        self.loading_jobs = 0  # Playlist / web downloads still running
        self.sid = None  # Web session id (outlives the stay in voice)
        self.last_active = _clock()
        self.text_channel = None  # Where the session was last announced
        self.started_at = None  # time.monotonic() when now_playing started
        self.paused_at = None  # time.monotonic() when paused, else None
        self.paused_total = 0.0  # Paused seconds since started_at
        # Serializes every change to the playback state (play_next, controls).
        self.lock = asyncio.Lock()
        # What play_next does when the current track stops: None (go on),
        # "next", "previous", "restart" or "seek". Set by controls before stop().
        self.pending_action = None
        # For "seek": where to restart (seconds) and whether to stay paused.
        self.seek_position = 0.0
        self.seek_paused = False
        # Bumped on each track start; a stale `after` callback is ignored.
        self.play_gen = 0
        self.crossfade = False  # Session setting, shared by everyone
        self.mixer = None  # CrossfadeMixer handed to the voice client
        # Status panel: the one message of the session that is edited in place.
        self.panel_message = None
        self.panel_text = None  # What the message currently shows
        self.panel_task = None  # Scheduled (throttled) refresh, if any
        self.panel_dirty = False
        self.panel_last_edit = float("-inf")  # time.monotonic() of last write
        self.panel_lock = asyncio.Lock()
        self.expired = False
        self.last_action = None  # Short line: "⏭️ Skipped via the web page"
        self.load_line = None  # Playlist progress / final result
        self.load_final = False  # load_line is a result, not progress
        self.single_downloads = 0  # Single-track downloads in progress

    @property
    def loading(self):
        return self.loading_jobs > 0

    def in_voice(self):
        return bool(self.voice_client and self.voice_client.is_connected())

    def elapsed(self):
        if self.started_at is None:
            return None
        end = self.paused_at if self.paused_at is not None else time.monotonic()
        return max(0.0, end - self.started_at - self.paused_total)

    def add_to_queue(self, track):
        if not self.should_stop:
            self.queue.append(track)

    def next_in_queue(self):
        if self.queue:
            return self.queue.pop(0)
        return None

    def stop_and_clear(self):
        self.should_stop = True
        self.epoch += 1
        self.play_gen += 1  # The stopped track's callback must not advance
        self.pending_action = None
        self.mixer = None
        tracks = list(self.queue) + list(self.history)
        if self.now_playing:
            tracks.append(self.now_playing)
        self.queue.clear()
        self.history.clear()
        self.now_playing = None
        if self.voice_client and (
            self.voice_client.is_playing() or self.voice_client.is_paused()
        ):
            self.voice_client.stop()
        discard_tracks(tracks)
        self.started_at = None
        self.paused_at = None
        self.paused_total = 0.0
        self.is_playing = False

    async def disconnect_voice_client(self):
        """Safely disconnect and cleanup voice client"""
        if self.voice_client:
            try:
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                if self.voice_client.is_connected():
                    await self.voice_client.disconnect()
            except Exception as e:
                print(f"Error during voice client disconnect: {e}")
            finally:
                self.voice_client = None


music_players = {}
# Web session id -> guild id. The only way a request reaches a player.
sessions = {}


def _file_in_use(filepath):
    """True if any player still references this file (the same video
    downloaded twice, or in two guilds, shares one path)."""
    for player in music_players.values():
        tracks = list(player.queue) + list(player.history)
        if player.now_playing:
            tracks.append(player.now_playing)
        if any(t.get("filepath") == filepath for t in tracks):
            return True
    return False


def discard_tracks(tracks):
    """Delete the files of tracks no player references any more."""
    for track in tracks:
        filepath = track.get("filepath") if track else None
        if not filepath or _file_in_use(filepath):
            continue
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except OSError as e:
            print(f"Could not delete {filepath}: {e}")


def push_history(player, track):
    player.history.append(track)
    evicted = []
    while len(player.history) > HISTORY_SIZE:
        evicted.append(player.history.pop(0))
    discard_tracks(evicted)


# --- Sessions ---------------------------------------------------------------


def _session_expired(player):
    if player.in_voice():
        return False  # Being in voice counts as activity
    return _clock() - player.last_active > SESSION_IDLE_HOURS * 3600


def expire_session(player):
    """End the session for good: its links stop working and its files go."""
    if player.sid:
        sessions.pop(player.sid, None)
        print(f"Web session expired: {player.sid}")
        player.sid = None
        player.expired = True
        refresh_panel(player)
    if not player.in_voice():
        tracks = list(player.queue) + list(player.history)
        if player.now_playing:
            tracks.append(player.now_playing)
        player.queue.clear()
        player.history.clear()
        player.now_playing = None
        discard_tracks(tracks)


def touch(player):
    """Record activity. An already expired session is ended, not revived."""
    if player.sid and _session_expired(player):
        expire_session(player)
    player.last_active = _clock()


def _session_player(sid):
    """The player behind a live session id, or None (expires lazily)."""
    guild_id = sessions.get(sid)
    if guild_id is None:
        return None
    player = music_players.get(guild_id)
    if not player or player.sid != sid:
        sessions.pop(sid, None)
        return None
    if player.in_voice():
        player.last_active = _clock()
    elif _session_expired(player):
        expire_session(player)
        return None
    return player


async def session_cleanup_loop():
    while True:
        await asyncio.sleep(SESSION_CLEANUP_SECONDS)
        try:
            for sid in list(sessions):
                _session_player(sid)
        except Exception as e:
            print(f"Session cleanup error: {e}")


# --- Themes -----------------------------------------------------------------
# Each guild picks the clip page's default theme (viewers can still override
# it locally). Stored in THEMES_FILE as {guild id: {skin, base, bg, text,
# accent, name, logo}}, logo being {"ext", "v"} or null; the logo itself is
# LOGO_DIR/<guild id>.<ext>. Every path comes from the guild id and a fixed
# extension, never from a request.

_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
guild_themes = {}
# `!theme edit` links: token -> (guild id, expiry on _clock()). In memory only.
edit_tokens = {}


def valid_color(value):
    return isinstance(value, str) and _COLOR_RE.fullmatch(value) is not None


def clean_theme_name(value):
    """The stripped community name, or None if it is not acceptable."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > THEME_NAME_MAX or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


def _clean_theme(raw):
    """A stored theme record with every bad or missing field dropped."""
    record = {}
    if not isinstance(raw, dict):
        return record
    if raw.get("skin") in THEME_SKINS:
        record["skin"] = raw["skin"]
    if raw.get("base") in THEME_PRESETS:
        record["base"] = raw["base"]
    for key in ("bg", "text", "accent"):
        if valid_color(raw.get(key)):
            record[key] = raw[key].lower()
    name = clean_theme_name(raw.get("name"))
    if name:
        record["name"] = name
    logo = raw.get("logo")
    if (
        isinstance(logo, dict)
        and logo.get("ext") in LOGO_TYPES
        and type(logo.get("v")) is int
    ):
        record["logo"] = {"ext": logo["ext"], "v": logo["v"]}
    return record


def load_themes():
    """(Re)load THEMES_FILE into guild_themes. A missing file is no error."""
    guild_themes.clear()
    try:
        with open(THEMES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"⚠️  Could not read {THEMES_FILE}: {e}")
        return
    if not isinstance(data, dict):
        return
    for key, raw in data.items():
        try:
            guild_id = int(key)
        except ValueError:
            continue
        record = _clean_theme(raw)
        if record:
            guild_themes[guild_id] = record


def _write_atomic(path, data):
    """Write bytes to `path` through a temp file and os.replace()."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_themes():
    """Persist guild_themes. Raises OSError (e.g. DATA_DIR not writable)."""
    data = {str(gid): record for gid, record in guild_themes.items()}
    _write_atomic(THEMES_FILE, json.dumps(data, indent=2).encode("utf-8"))


def theme_for(guild_id):
    """The guild's full theme, defaults filled in."""
    theme = {"skin": WEB_THEME, **PERSO_DEFAULTS, "name": "", "logo": None}
    theme.update(guild_themes.get(guild_id, {}))
    return theme


def theme_state(sid, guild_id):
    """The `theme` field of the web state."""
    theme = theme_for(guild_id)
    logo = theme.pop("logo")
    theme["logo_url"] = f"/theme-logo/{sid}?v={logo['v']}" if logo else None
    return theme


def logo_path(guild_id, ext):
    return os.path.join(LOGO_DIR, f"{int(guild_id)}.{ext}")


def sniff_image(data):
    """The extension of a PNG, JPEG, WebP or GIF by its magic bytes, else None.
    SVG (scriptable) and everything else is refused."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _remove_logo_files(guild_id, keep=None):
    for ext in LOGO_TYPES:
        if ext != keep:
            try:
                os.remove(logo_path(guild_id, ext))
            except FileNotFoundError:
                pass


def set_logo(guild_id, data, ext):
    """Store the guild's logo (replacing any previous one) and save."""
    _write_atomic(logo_path(guild_id, ext), data)
    _remove_logo_files(guild_id, keep=ext)
    record = guild_themes.setdefault(guild_id, {})
    record["logo"] = {"ext": ext, "v": int(time.time() * 1000)}
    save_themes()


def remove_logo(guild_id):
    record = guild_themes.get(guild_id)
    if record and record.pop("logo", None) is not None:
        save_themes()
    _remove_logo_files(guild_id)


def reset_theme(guild_id):
    """Back to WEB_THEME: drops the guild's colors, name and logo."""
    if guild_themes.pop(guild_id, None) is not None:
        save_themes()
    _remove_logo_files(guild_id)


def _prune_edit_tokens():
    now = _clock()
    for token, (_, expires) in list(edit_tokens.items()):
        if expires <= now:
            edit_tokens.pop(token, None)


def issue_edit_token(guild_id):
    _prune_edit_tokens()
    token = secrets.token_urlsafe(24)
    edit_tokens[token] = (guild_id, _clock() + THEME_EDIT_SECONDS)
    return token


def check_edit_token(given, guild_id):
    """True if `given` is a live edit token for this guild (constant-time)."""
    _prune_edit_tokens()
    if not isinstance(given, str) or not 0 < len(given) <= 128:
        return False
    given = given.encode("utf-8", "replace")
    ok = False
    for token, (gid, _) in list(edit_tokens.items()):
        if secrets.compare_digest(token.encode(), given) and gid == guild_id:
            ok = True
    return ok


load_themes()


async def open_session(ctx, player, announce=True):
    """Reuse the guild's live session or start one, and post its status panel."""
    touch(player)
    created = False
    if not player.sid:
        player.sid = secrets.token_urlsafe(16)
        sessions[player.sid] = ctx.guild.id
        player.expired = False
        created = True
        print(f"Web session opened for {ctx.guild.name}: {player.sid}")
    if not (created or announce):
        return
    player.text_channel = ctx.channel
    await post_panel(player)


# --- Status panel -----------------------------------------------------------
# One message per session, edited in place instead of posting an announcement
# for every event. Every state change calls refresh_panel().

PANEL_MIN_INTERVAL = 1.5  # Seconds between edits (Discord rate limits)
_NO_MENTIONS = discord.AllowedMentions.none()


def _short(title, limit=100):
    title = str(title)
    return title if len(title) <= limit else title[: limit - 1] + "…"


def render_panel(player):
    if player.expired:
        return "⌛ Session expired"
    lines = []
    if player.sid:
        lines.append(f"📺 Watch the clip live: {PUBLIC_BASE_URL}/{player.sid}")
    if not player.in_voice():
        lines.append(f"💤 Idle — {COMMAND_PREFIX}play to resume")
    elif player.now_playing:
        line = f"🎵 Now playing: **{_short(player.now_playing['title'])}**"
        if player.paused_at is not None:
            line += " ⏸️ paused"
        lines.append(line)
    else:
        lines.append("🎵 Nothing playing yet")
    if player.queue:
        line = f"⏭️ Up next: {_short(player.queue[0]['title'])}"
        if len(player.queue) > 1:
            line += f" and {len(player.queue) - 1} more"
        lines.append(line)
    if player.crossfade:
        lines.append("🔀 Crossfade on")
    if player.load_line:
        lines.append(player.load_line)
    elif player.single_downloads:
        lines.append("📥 Downloading…")
    if player.last_action:
        lines.append(player.last_action)
    return "\n".join(lines)


async def _send_panel(player):
    """Post the panel as a new message (caller holds panel_lock)."""
    text = render_panel(player)
    message = await player.text_channel.send(text, allowed_mentions=_NO_MENTIONS)
    player.panel_message = message
    player.panel_text = text
    player.panel_last_edit = time.monotonic()


async def post_panel(player):
    """Post a fresh panel at the bottom and delete the previous one."""
    if player.text_channel is None:
        return
    old = player.panel_message
    async with player.panel_lock:
        try:
            await _send_panel(player)
        except Exception as e:
            print(f"Could not post status panel: {e}")
            return
    if old is not None:
        try:
            await old.delete()
        except Exception:
            pass


async def _write_panel(player):
    async with player.panel_lock:
        text = render_panel(player)
        if player.panel_message is not None:
            if text == player.panel_text:
                return
            try:
                await player.panel_message.edit(
                    content=text, allowed_mentions=_NO_MENTIONS
                )
                player.panel_text = text
                return
            except discord.NotFound:
                player.panel_message = None  # Deleted by someone: post anew
            except Exception as e:
                print(f"Could not edit status panel: {e}")
                return
        if player.text_channel is None:
            return
        try:
            await _send_panel(player)
        except Exception as e:
            print(f"Could not post status panel: {e}")


async def _panel_worker(player):
    while player.panel_dirty:
        wait = player.panel_last_edit + PANEL_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        player.panel_dirty = False  # Changes during the edit trigger another
        await _write_panel(player)
        player.panel_last_edit = time.monotonic()


def refresh_panel(player):
    """Schedule a panel update. Coalesced: at most one edit per
    PANEL_MIN_INTERVAL, and the last state wins."""
    if player.text_channel is None:
        return
    player.panel_dirty = True
    if player.panel_task is not None and not player.panel_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    player.panel_task = loop.create_task(_panel_worker(player))


def set_action(player, text):
    """Show `text` as the panel's last-action line (None clears it)."""
    player.last_action = text
    if player.load_final:  # A finished load result lasts until the next change
        player.load_line, player.load_final = None, False
    refresh_panel(player)


async def ack(ctx):
    """React with a check mark on the user's command message."""
    message = getattr(ctx, "message", None)
    if message is None:
        return
    try:
        await message.add_reaction("✅")
    except Exception:
        pass


class _NullMessage:
    async def edit(self, **kwargs):
        pass

    async def delete(self):
        pass


class WebContext:
    """Stands in for a command context when the web page triggers work.
    It has no author, so it can never make the bot join voice."""

    author = None

    def __init__(self, guild_id, player):
        self.guild = bot.get_guild(guild_id) or types.SimpleNamespace(
            id=guild_id, name=""
        )
        self.channel = player.text_channel

    async def send(self, content=None, **kwargs):
        if self.channel is None:
            return _NullMessage()
        try:
            return await self.channel.send(content, **kwargs)
        except Exception as e:
            print(f"Could not send: {e}")
            return _NullMessage()


def get_video_id_from_url(url):
    """Return the specific video id (the `v=` param) if the URL points to one,
    else None. A URL like watch?v=XXX&list=YYY targets a specific song *inside*
    a playlist/radio, which is the ambiguous case we want to ask the user about."""
    try:
        query = parse_qs(urlparse(url).query)
        video_id = query.get("v", [None])[0]
        return video_id or None
    except Exception:
        return None


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_LIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")


def parse_youtube_url(url):
    """Validate a URL from the web page and rebuild it canonically.

    Returns ("single", url) or ("playlist", url), or None for anything that is
    not a YouTube video or playlist link. Only the rebuilt URL reaches yt-dlp,
    so the request cannot point it at another host.
    """
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        parsed.port  # Raises on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.port:
        return None
    query = parse_qs(parsed.query)
    if host == "youtu.be":
        video_id = parsed.path.strip("/")
    elif host in YOUTUBE_HOSTS:
        video_id = query.get("v", [""])[0]
        if not video_id and parsed.path.startswith("/shorts/"):
            video_id = parsed.path[len("/shorts/") :].strip("/")
        if not video_id:
            list_id = query.get("list", [""])[0]
            if _LIST_ID_RE.match(list_id):
                return ("playlist", f"https://www.youtube.com/playlist?list={list_id}")
            return None
    else:
        return None
    if _VIDEO_ID_RE.match(video_id):
        return ("single", f"https://www.youtube.com/watch?v={video_id}")
    return None


class PlaylistChoiceView(discord.ui.View):
    """Buttons shown when a link contains both a specific song and a playlist,
    letting the user pick just the song or the whole playlist."""

    def __init__(self, author_id, timeout=30):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.choice = None  # "single" | "playlist" | None (timeout)

    async def interaction_check(self, interaction):
        # Only the person who ran the command may use the buttons.
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ This isn't your command.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="This track only", emoji="🎵", style=discord.ButtonStyle.primary
    )
    async def single(self, interaction, button):
        self.choice = "single"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(
        label="Whole playlist", emoji="📋", style=discord.ButtonStyle.secondary
    )
    async def playlist(self, interaction, button):
        self.choice = "playlist"
        await interaction.response.defer()
        self.stop()


def get_playlist_info(url):
    """Extract playlist information without downloading."""
    try:
        print(f"Extracting playlist from: {url}")

        # Configuration to extract playlist
        ydl_opts = {
            "extract_flat": "in_playlist",  # Important: use 'in_playlist' for playlists
            "quiet": True,  # Reduce log spam
            "no_warnings": True,
            "ignoreerrors": True,  # Continue even if some videos have errors
            "playlistend": MAX_PLAYLIST_SIZE,  # Limit based on config
            "remote_components": REMOTE_COMPONENTS,
            "extractor_args": EXTRACTOR_ARGS,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if info is None:
                print("No information extracted")
                return []

            # Debug: display the type of info received
            info_type = info.get("_type", "video")
            print(f"Info type: {info_type}")
            print(f"Title: {info.get('title', 'Not found')}")

            # Handle different types of results
            if info_type == "playlist" or "entries" in info:
                entries = info.get("entries", [])

                if entries:
                    print(f"Number of entries found: {len(entries)}")

                    # Filter None entries and extract necessary info
                    valid_entries = []
                    for entry in entries:
                        if entry is not None:
                            processed_entry = {
                                "id": entry.get("id"),
                                "title": entry.get("title", "Unknown title"),
                                "url": entry.get("url")
                                or entry.get("webpage_url")
                                or f"https://www.youtube.com/watch?v={entry.get('id')}",
                                "webpage_url": entry.get("webpage_url")
                                or entry.get("url"),
                            }
                            if processed_entry["id"]:
                                valid_entries.append(processed_entry)

                    print(f"Number of valid entries: {len(valid_entries)}")
                    return valid_entries
                else:
                    print("Empty playlist or not found")
                    return []
            else:
                # It's a single video, not a playlist
                print("URL detected as single video, not a playlist")
                return []

    except Exception as e:
        print(f"Error extracting playlist: {e}")
        return []


# Simplified voice connection function to avoid error 4006
async def safe_voice_connect(voice_channel, max_attempts=3):
    """Secure voice connection that avoids error 4006"""
    guild = voice_channel.guild

    # Check if we already have a working connection to the same channel
    if guild.voice_client and guild.voice_client.is_connected():
        if guild.voice_client.channel == voice_channel:
            print(f"✅ Already connected to {voice_channel.name}")
            return guild.voice_client
        else:
            print(
                f"🔄 Switching from {guild.voice_client.channel.name} to {voice_channel.name}"
            )
            await guild.voice_client.disconnect(force=True)
            await asyncio.sleep(2)
    elif guild.voice_client:
        print(f"🔄 Cleaning up disconnected voice client")
        await guild.voice_client.disconnect(force=True)
        await asyncio.sleep(2)

    for attempt in range(max_attempts):
        try:
            print(
                f"🔌 Attempt {attempt + 1}/{max_attempts} to connect to {voice_channel.name}"
            )

            # Wait between attempts to avoid error 4006 and WebSocket issues
            if attempt > 0:
                wait_time = 5 + (attempt * 2)  # Longer waits for WebSocket stability
                print(f"⏳ Waiting {wait_time} seconds...")
                await asyncio.sleep(wait_time)

            # Connection with optimal parameters for Docker/WebSocket
            voice_client = await voice_channel.connect(
                timeout=90.0,  # Longer timeout for Docker
                reconnect=False,  # No auto reconnect to avoid loops
                self_deaf=True,  # Mute ourselves
                self_mute=False,  # But can speak (play music)
            )

            # Check that the connection works
            if voice_client and voice_client.is_connected():
                print(f"✅ Successfully connected to {voice_channel.name}")
                return voice_client
            else:
                print("⚠️ Connection established but not confirmed")
                if voice_client:
                    await voice_client.disconnect()

        except discord.errors.ConnectionClosed as e:
            if e.code == 4006:
                print(f"❌ Error 4006 detected (attempt {attempt + 1})")
                # For 4006, wait longer and clean completely
                await asyncio.sleep(5)
            else:
                print(f"❌ Connection error {e.code}: {e.reason}")

        except discord.ClientException as e:
            if "already connected" in str(e).lower():
                # If already connected, find and return the connection
                if guild.voice_client and guild.voice_client.channel == voice_channel:
                    print("✅ Existing connection found and reused")
                    return guild.voice_client
            print(f"❌ Client error: {e}")

        except Exception as e:
            error_msg = str(e)
            if "522" in error_msg or "Invalid response status" in error_msg:
                print(f"🌐 WebSocket error (attempt {attempt + 1}): {e}")
                print("💡 This is often caused by network connectivity issues")
            else:
                print(f"❌ Unexpected error: {e}")

    print("❌ All connection attempts failed")
    return None


async def join_voice_channel(ctx):
    """Simplified voice connection function"""
    author = getattr(ctx, "author", None)
    if not author or not author.voice:
        await ctx.send("❌ You must be in a voice channel!")
        return False

    voice_channel = author.voice.channel

    # Initialize player if necessary
    if ctx.guild.id not in music_players:
        music_players[ctx.guild.id] = MusicPlayer()

    player = music_players[ctx.guild.id]
    # Expire a stale session now: once in voice it would count as active.
    touch(player)

    current = ctx.guild.voice_client
    already_here = bool(
        current and current.is_connected() and current.channel == voice_channel
    )
    voice_client = await safe_voice_connect(voice_channel)

    if voice_client:
        player.voice_client = voice_client
        # A fresh join (re)posts the session link; a later !play does not.
        await open_session(ctx, player, announce=not already_here)
        return True
    else:
        await ctx.send("❌ Unable to connect to voice channel.")
        return False


def _remove_offtopic(ydl, info, filepath):
    """Cut SponsorBlock "music_offtopic" segments out of the file, in place.

    Best effort: no segments, an unreachable API or a failed cut leaves the
    file as downloaded. The cut is a stream copy; yt-dlp's concat output keeps
    audio and video aligned and writes the moov atom first (faststart).
    """
    pp_info = dict(info)
    for download in info.get("requested_downloads") or []:
        if download.get("filepath") == filepath:
            pp_info.update(download)
    pp_info["filepath"] = filepath
    try:
        _, pp_info = SponsorBlockPP(ydl, categories=SPONSORBLOCK_REMOVE).run(pp_info)
        cutter = ModifyChaptersPP(ydl, remove_sponsor_segments=SPONSORBLOCK_REMOVE)
        leftovers, _ = cutter.run(pp_info)
        for leftover in leftovers:
            if os.path.exists(leftover):
                os.remove(leftover)
    except Exception as e:
        print(f"SponsorBlock skipped: {e}")


def _parse_analysis(stderr):
    """Read silencedetect and loudnorm (print_format=json) output.

    Returns {"duration", "start", "end", "loudnorm"}; any part that cannot be
    read gets its neutral default (no trim, no normalisation).
    """
    duration = None
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if match:
        h, m, sec = match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(sec)

    silences = []  # [start, end or None]
    for kind, value in re.findall(r"silence_(start|end): (-?\d+(?:\.\d+)?)", stderr):
        if kind == "start":
            silences.append([float(value), None])
        elif silences and silences[-1][1] is None:
            silences[-1][1] = float(value)

    start, end = 0.0, duration
    if silences and silences[0][0] <= 0.05 and silences[0][1] is not None:
        start = min(silences[0][1], MAX_LEADING_TRIM)
    if silences and duration:
        tail_start, tail_end = silences[-1]
        if tail_start > 0.05 and (tail_end is None or tail_end >= duration - 0.1):
            end = tail_start
    if end is not None and end - start < MIN_PLAYED_SECONDS:
        start, end = 0.0, duration  # Mostly silence: play it untouched

    loudnorm = None
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr)
    if blocks:
        try:
            raw = json.loads(blocks[-1])
            values = {key: float(raw[key]) for key in LOUDNORM_KEYS}
            if all(math.isfinite(v) for v in values.values()):
                loudnorm = values
        except (ValueError, KeyError, TypeError):
            pass

    return {"duration": duration, "start": start, "end": end, "loudnorm": loudnorm}


def analyse_media(filepath):
    """One audio-only ffmpeg pass: loudness (loudnorm first pass) and silence.

    Blocking (a few seconds per track): call it from an executor thread.
    Never raises; a failure yields the defaults.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-vn",
        "-sn",
        "-dn",
        "-i",
        filepath,
        "-af",
        f"{SILENCE_FILTER},loudnorm={LOUDNORM_TARGET}:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=ANALYSIS_TIMEOUT,
        )
        return _parse_analysis(proc.stderr)
    except Exception as e:
        print(f"Audio analysis failed: {e}")
        return _parse_analysis("")


def _download_once(url):
    """Single download attempt. Raises on any failure."""
    # Ensure temp directory exists
    os.makedirs(temp_dir, exist_ok=True)

    # Simplified but robust configuration
    ydl_opts = {
        "format": MEDIA_FORMAT,
        "merge_output_format": "mp4",
        # Moov atom first, so browsers can start and seek before the end.
        "postprocessor_args": {"merger": ["-movflags", "+faststart"]},
        "noplaylist": True,
        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
        "quiet": False,
        "remote_components": REMOTE_COMPONENTS,
        "extractor_args": EXTRACTOR_ARGS,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        print(f"Downloading: {url}")
        info = ydl.extract_info(url, download=True)
        filepath = _find_download(info)
        _remove_offtopic(ydl, info, filepath)

    analysis = analyse_media(filepath)
    duration = analysis["duration"] or info.get("duration")
    duration = float(duration) if duration else None
    end = analysis["end"] if analysis["end"] is not None else duration
    print(
        f"Automix: start={analysis['start']:.2f}s end={end}s "
        f"loudnorm={'yes' if analysis['loudnorm'] else 'no'}"
    )
    return {
        "id": secrets.token_hex(4),
        "filepath": filepath,
        "title": info.get("title", "Unknown"),
        "duration": duration,  # Of the file, after the SponsorBlock cut
        "start": analysis["start"],  # File time where playback starts
        "end": end,  # File time where it stops (None: unknown, play it all)
        "loudnorm": analysis["loudnorm"],  # Measured values, or None
        "thumbnail": info.get("thumbnail"),
        "url": info.get("webpage_url") or url,
    }


def _find_download(info):
    """Path of the downloaded file. Raises if there is none."""

    def usable(filepath):
        return filepath and os.path.exists(filepath) and os.path.getsize(filepath) > 0

    # yt-dlp reports the final (merged) path
    for download in info.get("requested_downloads") or []:
        if usable(download.get("filepath")):
            return download["filepath"]

    # Fallback: find the downloaded file
    video_id = info.get("id", "")
    temp_files = os.listdir(temp_dir)
    for f in temp_files:
        if video_id in f and usable(os.path.join(temp_dir, f)):
            return os.path.join(temp_dir, f)

    # If not found by ID, take the most recent
    if temp_files:
        latest_file = max(
            temp_files,
            key=lambda f: os.path.getctime(os.path.join(temp_dir, f)),
        )
        if usable(os.path.join(temp_dir, latest_file)):
            return os.path.join(temp_dir, latest_file)

    raise RuntimeError("File not found after download")


def playback_options(track, offset, fade_in=None, fade_out=FADE_OUT_SECONDS):
    """FFmpeg options to play `track` from `offset` (file time, seconds).

    Returns (offset, kwargs for FFmpegPCMAudio); the offset is clamped to the
    automix window [start, end - 1]. Input -ss/-t keep FFmpeg in file time
    while the filters see a timeline starting at 0, so the fade-out is placed
    relative to the seek point. `fade_in` (None: automatic) and `fade_out`
    are overridden by the crossfade.
    """
    # The file carries video for the web page; Discord only needs audio.
    end = track.get("end")
    if end is None:  # Not analysed: play the whole file, untouched
        kwargs = {"options": "-vn"}
        if offset > 0:
            kwargs["before_options"] = f"-ss {offset:.3f}"
        return offset, kwargs

    start = track.get("start") or 0.0
    offset = max(start, min(offset, end - 1.0))
    remaining = end - offset
    filters = []
    measured = track.get("loudnorm")
    if measured:
        filters.append(
            f"loudnorm={LOUDNORM_TARGET}"
            f":measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}"
            ":linear=true"
        )
    if fade_in is None:
        fade_in = FADE_IN_SECONDS if offset <= start + 0.05 else SEEK_FADE_IN_SECONDS
    fade_in = min(fade_in, remaining / 2)
    fade_out = min(fade_out, remaining)
    filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    filters.append(f"afade=t=out:st={remaining - fade_out:.3f}:d={fade_out:.3f}")

    before = f"-ss {offset:.3f} " if offset > 0 else ""
    return offset, {
        "before_options": f"{before}-t {remaining:.3f}",
        "options": f"-vn -af {','.join(filters)}",
    }


def crossfade_eligible(track):
    """True if `track` is long enough (and analysed) to take part in a
    crossfade."""
    end = track.get("end") if track else None
    if end is None:
        return False
    return end - (track.get("start") or 0.0) >= CROSSFADE_MIN_TRACK


class CrossfadeMixer(discord.AudioSource):
    """The source handed to the voice client: plays one track's FFmpeg source
    and, once a crossfade is attached, mixes the tail of the outgoing track
    under the head of the next one.

    The FFmpeg filters do the fades, so mixing is a saturating sample-wise
    add, done only while two sources overlap. When the current track reaches
    its fade-out, `on_tail(mixer, gen)` is called once (on the audio thread)
    so the event loop can `attach()` the next track. A chain of crossfaded
    tracks is a single voice-client playback: the `after` callback fires once,
    when the last one ends, and reads `gen` to know which track that was.
    """

    def __init__(self, source, gen, frames=None, fade_frames=0, on_tail=None):
        self._lock = threading.Lock()
        self.source = source
        self.outgoing = None  # Previous track, draining under `source`
        self.gen = gen
        self._frames_left = frames  # Frames until `source` ends (None: unknown)
        self._fade_frames = fade_frames  # Length of its fade-out, in frames
        self._on_tail = on_tail
        self._tail_signalled = False
        self._pending = None  # First frame of an attached source, already read
        self.finished = False

    @property
    def _current_error(self):
        # Read by discord.py's AudioPlayer when the source ends.
        return getattr(self.source, "_current_error", None)

    def is_opus(self):
        return False

    def remaining_seconds(self):
        if self._frames_left is None:
            return None
        return self._frames_left / FRAMES_PER_SECOND

    def rearm(self):
        """Signal the tail again on the next frame (the crossfade was put off,
        e.g. by a pause)."""
        self._tail_signalled = False

    def attach(self, source, first_frame, gen, frames, fade_frames):
        """Start mixing `source` (whose first frame was already read) over the
        current one. False if playback has ended or a crossfade is running."""
        with self._lock:
            if self.finished or self.outgoing is not None:
                return False
            self.outgoing, self.source = self.source, source
            self._pending = first_frame
            self.gen = gen
            self._frames_left = frames
            self._fade_frames = fade_frames
            self._tail_signalled = False
            return True

    def read(self):
        with self._lock:
            if self.finished:
                return b""
            source, outgoing, data = self.source, self.outgoing, self._pending
            self._pending = None
        if data is None:
            data = source.read()
        if outgoing is not None:
            old = outgoing.read()
            if len(old) == PCM_FRAME_SIZE and len(data) == PCM_FRAME_SIZE:
                data = audioop.add(data, old, 2)  # Clips instead of wrapping
            elif not data:
                data = old
            if not old:  # The outgoing track is over: back to a single source
                with self._lock:
                    if self.outgoing is outgoing:
                        self.outgoing = None
                outgoing.cleanup()
        if not data:
            with self._lock:
                # A source attached just as `source` ended plays on.
                attached = self.source is not source
                if not attached:
                    self.finished = True
            return self.read() if attached else b""
        if self._frames_left is not None:
            self._frames_left -= 1
            if (
                not self._tail_signalled
                and self._on_tail is not None
                and self._fade_frames > 0
                and self._frames_left <= self._fade_frames
            ):
                self._tail_signalled = True
                try:
                    self._on_tail(self, self.gen)
                except Exception as e:
                    print(f"Crossfade signal failed: {e}")
        return data

    def cleanup(self):
        with self._lock:
            self.finished = True
            sources = [self.source, self.outgoing]
            self.outgoing = None
        for source in sources:
            if source is not None and hasattr(source, "cleanup"):
                try:
                    source.cleanup()
                except Exception as e:
                    print(f"Audio source cleanup failed: {e}")


def open_track_source(player, track, offset, fade_in=None):
    """Start FFmpeg for `track` from `offset`. Returns (offset, source, frames,
    fade_frames): the clamped offset, the source, its length in frames (None
    if unknown) and the length of its fade-out (0: cannot crossfade)."""
    eligible = crossfade_eligible(track)
    fade_out = CROSSFADE_SECONDS if player.crossfade and eligible else FADE_OUT_SECONDS
    offset, kwargs = playback_options(track, offset, fade_in, fade_out)
    source = discord.FFmpegPCMAudio(track["filepath"], **kwargs)
    end = track.get("end")
    if end is None:
        return offset, source, None, 0
    remaining = max(0.0, end - offset)
    frames = round(remaining * FRAMES_PER_SECOND)
    fade_frames = 0
    if eligible:
        # Toggled on mid-track, the fade already running is 4 s: overlap that.
        fade_frames = round(min(fade_out, remaining) * FRAMES_PER_SECOND)
    return offset, source, frames, fade_frames


def _is_permanent_failure(message):
    """True when retrying cannot help (deleted, private, blocked...)."""
    lowered = message.lower()
    return any(marker in lowered for marker in PERMANENT_ERROR_MARKERS)


def download_media(url):
    """Download one track (video+audio mp4), retrying transient YouTube refusals.

    Runs in an executor thread (see download_and_queue_song), so the blocking
    sleep between attempts does not stall the bot's event loop.
    """
    last_error = None

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            return _download_once(url)
        except Exception as e:
            last_error = str(e)
            print(
                f"Download error (attempt {attempt}/{DOWNLOAD_ATTEMPTS}): "
                f"{last_error}"
            )

            if _is_permanent_failure(last_error):
                print("Permanent failure, not retrying.")
                break
            if attempt == DOWNLOAD_ATTEMPTS:
                break

            delay = RETRY_BACKOFF_SECONDS[attempt - 1]
            print(f"Retrying in {delay}s...")
            time.sleep(delay)

    raise RuntimeError(f"Error during download: {last_error}")


def _cancelled(player, epoch):
    return player.should_stop or player.epoch != epoch


async def download_and_queue_song(
    ctx, player, url, skip_voice_check=False, from_web=False, quiet=False
):
    """Download one track and queue it. Returns the track, or None.
    `quiet` (playlist items) posts no message, not even on failure."""
    epoch = player.epoch
    try:
        if _cancelled(player, epoch):
            return None

        if not skip_voice_check:
            if not player.voice_client or not player.voice_client.is_connected():
                if not await join_voice_channel(ctx):
                    await ctx.send("❌ Unable to join voice channel!")
                    return None

        loop = asyncio.get_running_loop()
        if not quiet:
            player.single_downloads += 1
            refresh_panel(player)
        try:
            track = await loop.run_in_executor(None, download_media, url)
        finally:
            if not quiet:
                player.single_downloads = max(0, player.single_downloads - 1)

        if _cancelled(player, epoch):
            discard_tracks([track])
            refresh_panel(player)
            return None

        player.add_to_queue(track)
        if quiet:
            refresh_panel(player)
        else:
            set_action(player, f"➕ Added: {_short(track['title'])}")

        if not player.is_playing:
            await play_next(ctx)
        return track

    except Exception as e:
        if quiet:
            print(f"Playlist item failed: {e}")
            return None
        refresh_panel(player)
        await ctx.send(f"❌ Download error: {str(e)}")
        return None


async def queue_playlist(ctx, player, entries, from_web=False):
    """Download playlist entries one by one into the queue while earlier
    ones play. `loading` keeps play_next from leaving voice meanwhile.
    Progress lives in the status panel, not in channel messages."""
    epoch = player.epoch
    stopped = False
    loaded = 0
    total = len(entries)

    def progress(text, final=False):
        player.load_line, player.load_final = text, final
        refresh_panel(player)

    progress(f"📥 Loading 0/{total}…")
    player.loading_jobs += 1
    try:
        for entry in entries:
            if _cancelled(player, epoch):
                stopped = True
                break

            # Build video URL
            video_id = entry.get("id") or entry.get("url") or entry.get("webpage_url")
            if not video_id:
                continue

            if isinstance(video_id, str) and video_id.startswith("http"):
                video_url = video_id
            else:
                video_url = f"https://www.youtube.com/watch?v={video_id}"

            track = await download_and_queue_song(
                ctx, player, video_url, skip_voice_check=True, quiet=True
            )
            if track is not None and not _cancelled(player, epoch):
                loaded += 1
                progress(f"📥 Loading {loaded}/{total}…")
    finally:
        player.loading_jobs = max(0, player.loading_jobs - 1)

    if stopped or _cancelled(player, epoch):
        progress(f"⏹️ Loading stopped ({loaded}/{total})", final=True)
        return
    text = f"✅ {loaded}/{total} tracks loaded"
    if loaded < total:
        text += f" ({total - loaded} unavailable)"
    progress(text, final=True)
    # The last track may have finished while the queue was empty during
    # loading: start the queue again, or leave if there is nothing left.
    if not player.is_playing:
        await play_next(ctx)


async def play_next(ctx, gen=None, origin=None):
    """Advance playback. Called by the `after` callback (with the generation
    of the track that ended) or to start an idle player (gen=None)."""
    player = music_players.get(ctx.guild.id)
    if not player or (origin is not None and origin is not player):
        return  # Player replaced by !reset
    try:
        async with player.lock:
            if player.should_stop:
                return
            if gen is not None and gen != player.play_gen:
                return  # Stale callback
            if gen is None and player.is_playing:
                return  # Already playing; its callback will continue
            await _advance(ctx, player)
    except Exception as e:
        print(f"Error in play_next: {e}")
        player.is_playing = False
        await ctx.send(f"❌ Playback error: {str(e)}")


async def _advance(ctx, player):
    """Retire the current track according to pending_action and start the
    next one. Must run under player.lock."""
    action, player.pending_action = player.pending_action, None
    if action is None:
        player.last_action = None  # The track ended by itself
    finished = player.now_playing
    player.now_playing = None
    player.started_at = None
    player.paused_at = None
    player.paused_total = 0.0

    offset = 0.0
    keep_paused = False
    if action in ("restart", "seek") and finished:
        next_track = finished
        if action == "seek":
            offset, keep_paused = player.seek_position, player.seek_paused
    elif action == "previous" and finished and player.history:
        player.queue.insert(0, finished)
        next_track = player.history.pop()
    else:
        if finished:
            push_history(player, finished)
        next_track = player.next_in_queue()

    while next_track is not None and not os.path.exists(next_track["filepath"]):
        await ctx.send(f"❌ File not found for {next_track['title']}")
        next_track = player.next_in_queue()
        offset, keep_paused = 0.0, False  # The seek was for the lost track

    if next_track is None:
        player.is_playing = False
        # A playlist may still be downloading its next tracks. Stay connected
        # and idle instead of disconnecting; the next queued track will
        # trigger play_next() again once it finishes downloading.
        if player.loading:
            return
        if player.voice_client and player.voice_client.is_connected():
            await player.voice_client.disconnect()
        # The web session survives: it expires after SESSION_IDLE_HOURS.
        refresh_panel(player)
        return

    # Check voice connection
    if not player.voice_client or not player.voice_client.is_connected():
        if not await join_voice_channel(ctx):
            player.queue.insert(0, next_track)
            player.is_playing = False
            refresh_panel(player)
            await ctx.send("❌ Unable to join voice channel!")
            return

    player.play_gen += 1
    gen = player.play_gen
    loop = asyncio.get_running_loop()

    def after_playing(error):
        # Runs on the audio thread: only hand over to the event loop. A
        # crossfade chain ends on a later track than the one started here.
        if error:
            print(f"Playback error: {error}")
        try:
            asyncio.run_coroutine_threadsafe(play_next(ctx, mixer.gen, player), loop)
        except Exception as e:
            print(f"Error scheduling next song: {e}")

    def on_tail(source_mixer, tail_gen):
        # Audio thread, once per track, as its fade-out starts.
        if player.crossfade:
            asyncio.run_coroutine_threadsafe(
                _start_crossfade(player, source_mixer, tail_gen), loop
            )

    player.now_playing = next_track
    player.is_playing = True
    offset, audio_source, frames, fade_frames = open_track_source(
        player, next_track, offset
    )
    mixer = CrossfadeMixer(audio_source, gen, frames, fade_frames, on_tail)
    player.mixer = mixer
    try:
        player.voice_client.play(mixer, after=after_playing)
    except Exception:
        player.now_playing = None
        player.is_playing = False
        player.mixer = None
        mixer.cleanup()  # Its FFmpeg process is already running
        discard_tracks([next_track])
        raise
    now = time.monotonic()
    player.started_at = now - offset
    if keep_paused:
        player.voice_client.pause()
        player.paused_at = now
    player.last_active = _clock()
    refresh_panel(player)


async def _start_crossfade(player, mixer, gen):
    """Overlap the next queued track with the end of the current one.

    Called when the current track's fade-out starts. The next track becomes
    now_playing right away (history and queue change as for a normal advance)
    while the old one drains underneath; nothing calls play() or stop(), so
    the `after` callback fires once, at the end of the chain. Any explicit
    control stops the whole mixer and so cancels the overlap.
    """
    try:
        async with player.lock:
            if (
                not player.crossfade
                or player.should_stop
                or gen != player.play_gen
                or player.mixer is not mixer
                or player.pending_action is not None
                or not player.is_playing
                or not player.in_voice()
                or not player.queue
            ):
                return
            if player.paused_at is not None:
                mixer.rearm()  # Try again once resumed
                return
            current, upcoming = player.now_playing, player.queue[0]
            if not (crossfade_eligible(current) and crossfade_eligible(upcoming)):
                return
            if not os.path.exists(upcoming["filepath"]):
                return  # The normal advance reports it
            overlap = mixer.remaining_seconds()
            if overlap is None or overlap < 1.0:
                return  # Too late: let it end normally
            offset, source, frames, fade_frames = open_track_source(
                player, upcoming, 0.0, fade_in=overlap
            )
            # FFmpeg needs a moment to start; read its first frame here so
            # the audio thread never waits for it.
            loop = asyncio.get_running_loop()
            try:
                first = await loop.run_in_executor(None, source.read)
            except Exception:
                source.cleanup()
                raise
            if not first or not mixer.attach(
                source, first, player.play_gen + 1, frames, fade_frames
            ):
                source.cleanup()
                return
            player.play_gen += 1
            player.queue.remove(upcoming)
            push_history(player, current)
            player.now_playing = upcoming
            player.started_at = time.monotonic() - offset
            player.paused_at = None
            player.paused_total = 0.0
            player.last_action = None
            player.last_active = _clock()
            refresh_panel(player)
    except Exception as e:
        print(f"Crossfade failed: {e}")


# --- Controls shared by Discord commands and the web page -------------------


class ControlError(Exception):
    """A control that cannot be applied. `code` picks the message."""

    # code -> (HTTP status, French message for the web page, English message)
    MESSAGES = {
        "not_in_voice": (
            409,
            "Le bot n'est pas dans un salon vocal.",
            "I'm not in a voice channel.",
        ),
        "nothing_playing": (
            409,
            "Rien n'est en cours de lecture.",
            "No music is currently playing.",
        ),
        "track_not_found": (
            404,
            "Ce morceau n'est plus dans la file.",
            "That track is not in the queue.",
        ),
        "bad_index": (400, "Position invalide.", "Invalid position."),
        "bad_url": (
            400,
            "Seuls les liens YouTube (vidéo ou playlist) sont acceptés.",
            "Only YouTube video or playlist links are accepted.",
        ),
    }

    def __init__(self, code):
        super().__init__(code)
        self.code = code
        self.status, self.french, self.english = self.MESSAGES[code]


def _require_playing(player):
    if not player.in_voice():
        raise ControlError("not_in_voice")
    if not player.is_playing or not player.now_playing:
        raise ControlError("nothing_playing")


async def _stop_with(player, action):
    """Stop the current track; play_next then applies `action`."""
    async with player.lock:
        _require_playing(player)
        if player.pending_action is not None:
            return  # A transition is already under way
        player.pending_action = action
        player.voice_client.stop()


async def control_next(player):
    await _stop_with(player, "next")


async def control_previous(player):
    """Restart the current track, or go back to the last played one.
    Returns "restart" or "previous"."""
    elapsed = player.elapsed() or 0.0
    if player.now_playing:  # Played time counts from the automix start
        elapsed -= player.now_playing.get("start") or 0.0
    if elapsed > PREVIOUS_RESTART_SECONDS or not player.history:
        action = "restart"
    else:
        action = "previous"
    await _stop_with(player, action)
    return action


async def control_seek(player, position):
    """Restart the current track at `position` seconds (clamped to the
    track), keeping it paused if it was. Returns the applied position."""
    async with player.lock:
        _require_playing(player)
        position = max(0.0, float(position))
        track = player.now_playing
        start, end = track.get("start") or 0.0, track.get("end")
        if end is not None:  # Automix window, as playback_options clamps it
            position = max(start, min(position, end - 1))
        elif track.get("duration"):
            position = max(0.0, min(position, track["duration"] - 1))
        if player.pending_action == "seek":
            player.seek_position = position  # Not applied yet: retarget it
            return position
        if player.pending_action is not None:
            return position  # Another transition is already under way
        player.seek_position = position
        player.seek_paused = player.paused_at is not None
        player.pending_action = "seek"
        player.voice_client.stop()
        return position


async def control_jump(player, guild_id, track_id):
    """Play a queued track now; the current one goes to history like a skip
    and the tracks queued before it keep their order. Returns the track."""
    async with player.lock:
        if not player.in_voice():
            raise ControlError("not_in_voice")
        track = player.queue.pop(_find_queued(player, track_id))
        player.queue.insert(0, track)
        idle = not player.is_playing or not player.now_playing
        if not idle:
            # A pending action is only applied by play_next, after this
            # stop(): overriding it is safe and makes the jump win.
            already_stopping = player.pending_action is not None
            player.pending_action = "next"
            if not already_stopping:
                player.voice_client.stop()
    if idle:
        await play_next(WebContext(guild_id, player))
    return track


async def control_pause(player):
    """Returns False if it was already paused."""
    async with player.lock:
        _require_playing(player)
        if player.paused_at is not None:
            return False
        player.voice_client.pause()
        player.paused_at = time.monotonic()
        return True


async def control_resume(player):
    """Returns False if it was not paused."""
    async with player.lock:
        _require_playing(player)
        if player.paused_at is None:
            return False
        player.voice_client.resume()
        player.paused_total += time.monotonic() - player.paused_at
        player.paused_at = None
        return True


def control_crossfade(player, enabled):
    """Turn the session's crossfade on or off. It applies from the next
    transition; an overlap already running finishes."""
    player.crossfade = bool(enabled)
    player.last_active = _clock()
    refresh_panel(player)


def seek_label(player, applied):
    """Panel line for a seek; the time is relative to the track's automix
    start, as the web page shows it."""
    start = (player.now_playing or {}).get("start") or 0.0
    rel = int(max(0.0, applied - start))
    return f"⏩ Seeked to {rel // 60}:{rel % 60:02d}"


def _find_queued(player, track_id):
    for i, track in enumerate(player.queue):
        if track["id"] == track_id:
            return i
    raise ControlError("track_not_found")


async def control_remove(player, track_id):
    """Returns the removed track."""
    async with player.lock:
        if not player.in_voice():
            raise ControlError("not_in_voice")
        track = player.queue.pop(_find_queued(player, track_id))
    discard_tracks([track])
    return track


async def control_move(player, track_id, index):
    """Move a queued track to a 0-based queue index. Returns the track."""
    async with player.lock:
        if not player.in_voice():
            raise ControlError("not_in_voice")
        i = _find_queued(player, track_id)
        if not 0 <= index < len(player.queue):
            raise ControlError("bad_index")
        track = player.queue.pop(i)
        player.queue.insert(index, track)
    return track


_background_tasks = set()


def control_add(player, guild_id, url):
    """Validate a link and download it in the background."""
    parsed = parse_youtube_url(url)
    if parsed is None:
        raise ControlError("bad_url")
    if not player.in_voice():
        raise ControlError("not_in_voice")
    kind, clean_url = parsed
    task = asyncio.get_running_loop().create_task(
        _add_from_web(player, guild_id, kind, clean_url)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _add_from_web(player, guild_id, kind, url):
    ctx = WebContext(guild_id, player)
    epoch = player.epoch
    # Counts as loading, so the queue running out meanwhile keeps the bot in
    # voice (the web page cannot make it join again).
    player.loading_jobs += 1
    try:
        if kind == "single":
            await download_and_queue_song(
                ctx, player, url, skip_voice_check=True, from_web=True
            )
        else:
            loop = asyncio.get_running_loop()
            entries = await loop.run_in_executor(None, get_playlist_info, url)
            if _cancelled(player, epoch):
                pass
            elif not entries:
                await ctx.send(
                    "❌ Could not read the playlist added from the web page."
                )
            else:
                entries = entries[:MAX_PLAYLIST_SIZE]
                await queue_playlist(ctx, player, entries, from_web=True)
    except Exception as e:
        print(f"Web add failed: {e}")
    finally:
        player.loading_jobs = max(0, player.loading_jobs - 1)
    if not player.is_playing and not _cancelled(player, epoch):
        await play_next(ctx)


@bot.event
async def on_ready():
    print(f"{bot.user} is connected and ready!")
    print(f"Bot ID: {bot.user.id}")
    print(f"Connected to {len(bot.guilds)} guilds")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{COMMAND_PREFIX}help for command list",
        )
    )


@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes for cleanup"""
    if member == bot.user and before.channel and not after.channel:
        print(f"Bot was disconnected from voice channel: {before.channel.name}")

        guild = before.channel.guild
        # The client's own handler runs in a separate task, so give it time to
        # settle. A stale event from an old disconnect (!stop then !play) then
        # sees a live client and is ignored; a real kick sees it disconnected.
        await asyncio.sleep(1.5)
        if guild.voice_client and guild.voice_client.is_connected():
            return
        if guild.id in music_players:
            player = music_players[guild.id]
            player.voice_client = None
            player.is_playing = False
            refresh_panel(player)
            # The web session stays; it expires after SESSION_IDLE_HOURS.


@bot.before_invoke
async def record_command_activity(ctx):
    if ctx.guild and ctx.guild.id in music_players:
        touch(music_players[ctx.guild.id])


@bot.command(name="play")
async def play(ctx, url: str = None):
    if url is None:
        await ctx.send(f"❌ Usage: `{COMMAND_PREFIX}play <YouTube URL>`")
        return

    if not await join_voice_channel(ctx):
        return

    if ctx.guild.id not in music_players:
        music_players[ctx.guild.id] = MusicPlayer()

    player = music_players[ctx.guild.id]
    player.should_stop = False

    try:
        await ack(ctx)
        player.load_line, player.load_final = "🔎 Analyzing the link…", False
        refresh_panel(player)

        # Try to extract as playlist
        loop = asyncio.get_running_loop()
        try:
            playlist_entries = await loop.run_in_executor(None, get_playlist_info, url)
        finally:
            player.load_line = None
            refresh_panel(player)

        if playlist_entries:
            # It's a playlist
            # If the link targets a specific song *and* carries a playlist/radio
            # (e.g. watch?v=XXX&list=RD...), ask the user what they want.
            specific_video_id = get_video_id_from_url(url)
            if specific_video_id:
                view = PlaylistChoiceView(ctx.author.id)
                prompt = await ctx.send(
                    f"🎧 This link contains a playlist of **{len(playlist_entries)}** tracks.\n"
                    "Do you want just this track or the whole playlist?",
                    view=view,
                )
                await view.wait()
                try:
                    await prompt.delete()
                except Exception:
                    pass

                # Default to the single track on timeout (choice is None).
                if view.choice != "playlist":
                    single_url = f"https://www.youtube.com/watch?v={specific_video_id}"
                    if not player.should_stop:
                        await download_and_queue_song(ctx, player, single_url)
                    return
                # Otherwise fall through and load the whole playlist.

            # Make sure we're connected
            if not player.voice_client or not player.voice_client.is_connected():
                if not await join_voice_channel(ctx):
                    await ctx.send("❌ Unable to join voice channel!")
                    return

            # Limit based on configuration
            playlist_entries = playlist_entries[:MAX_PLAYLIST_SIZE]
            await queue_playlist(ctx, player, playlist_entries)
        else:
            # It's a single video
            if not player.should_stop:
                await download_and_queue_song(ctx, player, url)

    except Exception as e:
        await ctx.send(f"❌ An error occurred: {str(e)}")


@bot.command(name="stop")
async def stop(ctx):
    player = music_players.get(ctx.guild.id)
    if not player:
        await ctx.send("❌ No music is currently playing.")
        return

    async with player.lock:
        player.stop_and_clear()
    await player.disconnect_voice_client()
    # The web session stays (same link on the next !play) until it expires.
    if not player.loading:
        player.load_line, player.load_final = None, False
    set_action(player, "⏹️ Stopped")
    await ack(ctx)


@bot.command(name="reset")
async def reset_voice(ctx):
    """Force reset the voice connection"""
    await ctx.send("🔧 Resetting voice connection...")

    player = music_players.get(ctx.guild.id)

    # Disconnect completely
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect(force=True)

    # Clean the player. The web session survives a reset: same link.
    if player:
        player.should_stop = True
        player.epoch += 1
        await player.disconnect_voice_client()
        fresh = MusicPlayer()
        fresh.sid = player.sid
        fresh.text_channel = player.text_channel
        fresh.last_active = player.last_active
        fresh.history = player.history
        fresh.panel_message = player.panel_message
        fresh.panel_text = player.panel_text
        fresh.expired = player.expired
        fresh.crossfade = player.crossfade
        if player.panel_task is not None:
            player.panel_task.cancel()
        music_players[ctx.guild.id] = fresh
        leftovers = list(player.queue)
        if player.now_playing:
            leftovers.append(player.now_playing)
        player.queue = []
        player.history = []
        player.now_playing = None
        discard_tracks(leftovers)

    # Wait a bit (the voice state update arrives in the meantime)
    await asyncio.sleep(2)
    if player:
        refresh_panel(music_players[ctx.guild.id])
    await ctx.send("✅ Voice connection reset!")


@bot.command(name="list")
async def list_queue(ctx):
    player = music_players.get(ctx.guild.id)
    if not player or not player.queue:
        await ctx.send("❌ The queue is empty.")
        return

    queue_items = [f"{i+1}. {song['title']}" for i, song in enumerate(player.queue)]
    message = "🎶 **Queue:**\n" + "\n".join(queue_items[:10])

    if len(player.queue) > 10:
        message += f"\n... and {len(player.queue) - 10} more tracks"

    await ctx.send(message)


_FAILED = object()


async def _run_command_control(ctx, control):
    """Run a control for a Discord command; returns its result (possibly
    None) or _FAILED after replying with the error."""
    player = music_players.get(ctx.guild.id)
    if not player:
        await ctx.send("❌ No music is currently playing.")
        return _FAILED
    try:
        result = await control(player)
    except ControlError as e:
        await ctx.send(f"❌ {e.english}")
        return _FAILED
    await ack(ctx)
    refresh_panel(player)
    return result


@bot.command(name="next")
async def next_track(ctx):
    # Stopping triggers the after_playing callback, which plays the next track.
    if await _run_command_control(ctx, control_next) is not _FAILED:
        set_action(music_players[ctx.guild.id], "⏭️ Skipped")


def parse_position(text):
    """Seconds from "90", "1:30" or "1:02:03"; None if invalid."""
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(not math.isfinite(p) or p < 0 for p in parts):
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


@bot.command(name="seek")
async def seek(ctx, position: str):
    seconds = parse_position(position)
    if seconds is None:
        await ctx.send("❌ Invalid position. Use seconds or mm:ss.")
        return

    def relative_seek(player):
        # The position counts from the automix start, as the page shows it.
        start = (player.now_playing or {}).get("start") or 0.0
        return control_seek(player, start + seconds)

    applied = await _run_command_control(ctx, relative_seek)
    if applied is not _FAILED:
        player = music_players[ctx.guild.id]
        set_action(player, seek_label(player, applied))


@bot.command(name="crossfade")
async def crossfade(ctx, mode: str = None):
    mode = (mode or "").lower()
    if mode not in ("on", "off"):
        await ctx.send(f"❌ Usage: `{COMMAND_PREFIX}crossfade on|off`")
        return
    player = music_players.setdefault(ctx.guild.id, MusicPlayer())
    touch(player)
    control_crossfade(player, mode == "on")
    await ack(ctx)


@bot.command(name="theme")
@commands.has_guild_permissions(manage_guild=True)
async def theme(ctx, choice: str = None):
    """Show or set the clip page's default theme for this server."""
    guild_id = ctx.guild.id
    choice = (choice or "").strip().lower()
    player = music_players.get(guild_id)
    if not choice:
        current = theme_for(guild_id)["skin"]
        source = "server default" if guild_id in guild_themes else "fallback"
        await ctx.send(
            f"🎨 Clip page theme: **{current}** ({source})\n"
            f"Presets: {', '.join(THEME_SKINS)}\n"
            f"`{COMMAND_PREFIX}theme <preset>` sets it, "
            f"`{COMMAND_PREFIX}theme reset` restores `{WEB_THEME}`, "
            f"`{COMMAND_PREFIX}theme edit` sends you an editor link "
            "(custom colors, name, logo)."
        )
        return

    if choice == "edit":
        player = music_players.setdefault(guild_id, MusicPlayer())
        if player.sid and _session_player(player.sid) is None:
            player.sid = None  # Expired: open_session makes a fresh one
        # The editor lives on the session's page: open one if needed (as a
        # join does, without joining voice).
        await open_session(ctx, player, announce=False)
        token = issue_edit_token(guild_id)
        try:
            await ctx.author.send(
                f"🎨 Theme editor for **{ctx.guild.name}** (valid "
                f"{THEME_EDIT_SECONDS // 60} min, do not share it):\n"
                f"{PUBLIC_BASE_URL}/{player.sid}?edit={token}"
            )
        except discord.HTTPException:
            edit_tokens.pop(token, None)
            await ctx.send(
                "❌ I can't send you a direct message. Allow DMs from server "
                f"members, then run `{COMMAND_PREFIX}theme edit` again."
            )
            return
        await ack(ctx)
        return

    try:
        if choice == "reset":
            reset_theme(guild_id)
            label = WEB_THEME
        elif choice in THEME_SKINS:
            guild_themes.setdefault(guild_id, {})["skin"] = choice
            save_themes()
            label = choice
        else:
            await ctx.send(
                f"❌ Unknown theme. Presets: {', '.join(THEME_SKINS)}, "
                "or `reset` / `edit`."
            )
            return
    except OSError as e:
        print(f"Could not save theme: {e}")
        await ctx.send("❌ Could not save the theme (is DATA_DIR writable?).")
        return
    await ack(ctx)
    if player:
        set_action(player, f"🎨 Clip page theme: {label}")


@bot.command(name="pause")
async def pause(ctx):
    await _run_command_control(ctx, control_pause)


@bot.command(name="resume")
async def resume(ctx):
    await _run_command_control(ctx, control_resume)


@bot.event
async def on_command_error(ctx, error):
    """Global command error handler to avoid ugly tracebacks."""
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Try `{COMMAND_PREFIX}help`.")
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("❌ This command only works in a server.")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(
            f"❌ You need the **Manage Server** permission to use "
            f"`{COMMAND_PREFIX}{ctx.command}`."
        )
        return
    print(f"Command error in '{ctx.command}': {error}")
    try:
        await ctx.send(f"❌ Error: {error}")
    except Exception:
        pass


# --- Web server: synced clip page -------------------------------------------
#
# Contract relied on by web/index.html:
#   GET /                      landing text
#   GET /{sid}                 the page (it handles unknown/ended sessions)
#   GET /api/{sid}/state       session state JSON, 404 {"active": false} if none
#   POST /api/{sid}/control    {"action", "id"?, "index"?, "url"?, "position"?,
#                              "enabled"?} -> {"ok": ...}; actions: next,
#                              previous, pause, resume, remove, move, add,
#                              seek, jump, crossfade ({"enabled": bool})
#   GET /media/{sid}/{id}      the mp4 of a playing, queued or history track
#   POST /api/{sid}/theme      {skin, base, bg, text, accent, name}, with the
#                              X-Edit-Token header of a `!theme edit` link
#   POST|DELETE /api/{sid}/theme/logo   raw PNG/JPEG/WebP/GIF body (same token)
#   GET /theme-logo/{sid}      the guild's logo (?v= busts caches)
# Paths are never built from the request: tracks are looked up by id, themes
# and logos by the session's guild id.


def _track_summary(track):
    return {
        "id": track["id"],
        "title": track["title"],
        "duration": track.get("duration"),
        "thumbnail": track.get("thumbnail"),
        "url": track.get("url"),
    }


async def web_landing(request):
    return web.Response(text="DJ Snake")


async def web_page(request):
    if not os.path.isfile(WEB_INDEX):
        return web.Response(status=404, text="Page not found")
    # The URL may carry a `!theme edit` token: never leak it as a referrer.
    headers = {"Cache-Control": "no-cache", "Referrer-Policy": "same-origin"}
    return web.FileResponse(WEB_INDEX, headers=headers)


async def web_state(request):
    sid = request.match_info["sid"]
    headers = {"Cache-Control": "no-store"}
    player = _session_player(sid)
    if player is None:
        return web.json_response({"active": False}, status=404, headers=headers)

    guild = bot.get_guild(sessions[sid])
    in_voice = player.in_voice()
    channel = None
    now_playing = None
    queue = []
    if in_voice:
        if player.voice_client.channel:
            channel = player.voice_client.channel.name
        if player.now_playing and player.started_at is not None:
            track = player.now_playing
            now_playing = _track_summary(track)
            now_playing["video_url"] = f"/media/{sid}/{track['id']}"
            now_playing["elapsed"] = player.elapsed()  # File time
            # Automix window (file time): the page's progress bar spans it.
            now_playing["start"] = track.get("start") or 0.0
            now_playing["end"] = track.get("end") or track.get("duration")
        queue = [_track_summary(t) for t in player.queue]

    return web.json_response(
        {
            "active": True,
            "in_voice": in_voice,
            "guild": guild.name if guild else "",
            "channel": channel,
            "loading": player.loading,
            "paused": now_playing is not None and player.paused_at is not None,
            "can_previous": now_playing is not None and player.pending_action is None,
            "crossfade": player.crossfade,
            # True while the previous track still drains under this one.
            "crossfading": now_playing is not None
            and player.mixer is not None
            and player.mixer.outgoing is not None,
            "server_time": time.time(),
            "now_playing": now_playing,
            "queue": queue,
            "theme": theme_state(sid, sessions[sid]),
        },
        headers=headers,
    )


async def web_media(request):
    sid = request.match_info["sid"]
    track_id = request.match_info["track_id"]
    player = _session_player(sid)
    if player is None:
        raise web.HTTPNotFound()
    tracks = ([player.now_playing] if player.now_playing else []) + list(player.queue)
    tracks += list(player.history)
    for track in tracks:
        if track.get("id") == track_id:
            filepath = track["filepath"]
            if os.path.isfile(filepath):
                return web.FileResponse(filepath)
            break
    raise web.HTTPNotFound()


def _control_error(status, message):
    return web.json_response({"ok": False, "error": message}, status=status)


async def _read_body(request, limit):
    """The request body, or None if it is larger than `limit` bytes."""
    if (request.content_length or 0) > limit:
        return None
    # StreamReader.read() may return a partial chunk: read until EOF.
    body = b""
    while len(body) <= limit:
        chunk = await request.content.read(limit + 1 - len(body))
        if not chunk:
            break
        body += chunk
    return None if len(body) > limit else body


async def _read_json(request):
    """(dict, None) or (None, error response) for a small JSON body."""
    body = await _read_body(request, MAX_CONTROL_BODY)
    if body is None:
        return None, _control_error(400, "Requête trop volumineuse.")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None, _control_error(400, "Requête invalide.")
    if not isinstance(data, dict):
        return None, _control_error(400, "Requête invalide.")
    return data, None


async def web_control(request):
    sid = request.match_info["sid"]
    player = _session_player(sid)
    if player is None:
        return _control_error(404, "Session inconnue ou expirée.")
    guild_id = sessions[sid]

    data, error = await _read_json(request)
    if error is not None:
        return error
    action = data.get("action")
    if action not in (
        "next",
        "previous",
        "pause",
        "resume",
        "remove",
        "move",
        "add",
        "seek",
        "jump",
        "crossfade",
    ):
        return _control_error(400, "Action inconnue.")

    track_id = data.get("id")
    index = data.get("index")
    if action in ("remove", "move", "jump") and not (
        isinstance(track_id, str) and 0 < len(track_id) <= 64
    ):
        return _control_error(400, "Morceau invalide.")
    if action == "move" and (type(index) is not int):
        return _control_error(400, "Position invalide.")
    position = data.get("position")
    if action == "seek" and not (
        isinstance(position, (int, float))
        and not isinstance(position, bool)
        and math.isfinite(position)
        and position >= 0
    ):
        return _control_error(400, "Position invalide.")
    if action == "add" and not isinstance(data.get("url"), str):
        return _control_error(400, "Lien manquant.")
    if action == "crossfade" and not isinstance(data.get("enabled"), bool):
        return _control_error(400, "Requête invalide.")

    touch(player)
    if player.sid != sid:  # Expired just now
        return _control_error(404, "Session inconnue ou expirée.")

    try:
        if action == "next":
            await control_next(player)
            set_action(player, "⏭️ Skipped via the web page")
        elif action == "previous":
            if await control_previous(player) == "restart":
                set_action(player, "⏮️ Restarted via the web page")
            else:
                set_action(player, "⏮️ Previous track via the web page")
        elif action == "pause":
            await control_pause(player)
            refresh_panel(player)
        elif action == "resume":
            await control_resume(player)
            refresh_panel(player)
        elif action == "remove":
            track = await control_remove(player, track_id)
            set_action(player, f"🗑️ Removed: {_short(track['title'])}")
        elif action == "move":
            await control_move(player, track_id, index)
            set_action(player, "🔀 Queue reordered")
        elif action == "seek":
            applied = await control_seek(player, position)
            set_action(player, seek_label(player, applied))
        elif action == "crossfade":
            control_crossfade(player, data["enabled"])
        elif action == "jump":
            track = await control_jump(player, guild_id, track_id)
            set_action(player, f"▶️ Jumped to: {_short(track['title'])}")
        elif action == "add":
            control_add(player, guild_id, data["url"])
            return web.json_response({"ok": True}, status=202)
    except ControlError as e:
        return _control_error(e.status, e.french)
    return web.json_response({"ok": True})


def _theme_auth(request):
    """(player, guild id, None) for a valid edit token on a live session,
    else (None, None, error response)."""
    sid = request.match_info["sid"]
    player = _session_player(sid)
    if player is None:
        return None, None, _control_error(404, "Session inconnue ou expirée.")
    guild_id = sessions[sid]
    if not check_edit_token(request.headers.get("X-Edit-Token"), guild_id):
        return (
            None,
            None,
            _control_error(
                403,
                "Lien d'édition invalide ou expiré. "
                "Relance !theme edit dans Discord.",
            ),
        )
    return player, guild_id, None


def _theme_saved(request, player, guild_id):
    touch(player)
    set_action(player, "🎨 Clip page theme updated via the web page")
    theme = theme_state(request.match_info["sid"], guild_id)
    return web.json_response({"ok": True, "theme": theme})


_SAVE_FAILED = "Enregistrement impossible sur le serveur."


async def web_theme(request):
    player, guild_id, error = _theme_auth(request)
    if error is not None:
        return error
    data, error = await _read_json(request)
    if error is not None:
        return error
    if data.get("skin") not in THEME_SKINS:
        return _control_error(400, "Thème inconnu.")
    if data.get("base") not in THEME_PRESETS:
        return _control_error(400, "Style de base inconnu.")
    if not all(valid_color(data.get(k)) for k in ("bg", "text", "accent")):
        return _control_error(400, "Couleur invalide (format #rrggbb).")
    name = clean_theme_name(data.get("name", ""))
    if name is None:
        return _control_error(
            400, f"Nom invalide ({THEME_NAME_MAX} caractères maximum)."
        )
    record = guild_themes.setdefault(guild_id, {})
    record.update(
        skin=data["skin"],
        base=data["base"],
        bg=data["bg"].lower(),
        text=data["text"].lower(),
        accent=data["accent"].lower(),
        name=name,
    )
    try:
        save_themes()
    except OSError as e:
        print(f"Could not save theme: {e}")
        return _control_error(500, _SAVE_FAILED)
    return _theme_saved(request, player, guild_id)


async def web_theme_logo(request):
    player, guild_id, error = _theme_auth(request)
    if error is not None:
        return error
    if request.method == "DELETE":
        try:
            remove_logo(guild_id)
        except OSError as e:
            print(f"Could not remove logo: {e}")
            return _control_error(500, _SAVE_FAILED)
        return _theme_saved(request, player, guild_id)
    body = await _read_body(request, MAX_LOGO_BYTES)
    if body is None:
        return _control_error(400, "Image trop lourde (512 Ko maximum).")
    ext = sniff_image(body)
    if ext is None:
        return _control_error(
            400, "Format non pris en charge (PNG, JPEG, WebP ou GIF)."
        )
    try:
        set_logo(guild_id, body, ext)
    except OSError as e:
        print(f"Could not save logo: {e}")
        return _control_error(500, _SAVE_FAILED)
    return _theme_saved(request, player, guild_id)


async def web_theme_logo_file(request):
    sid = request.match_info["sid"]
    if _session_player(sid) is None:
        raise web.HTTPNotFound()
    guild_id = sessions[sid]
    logo = guild_themes.get(guild_id, {}).get("logo")
    if not logo:
        raise web.HTTPNotFound()
    try:
        with open(logo_path(guild_id, logo["ext"]), "rb") as f:
            data = f.read(MAX_LOGO_BYTES + 1)
    except OSError:
        raise web.HTTPNotFound()
    return web.Response(
        body=data,
        content_type=LOGO_TYPES[logo["ext"]],
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
            "Cache-Control": "public, max-age=86400",
        },
    )


def build_web_app():
    app = web.Application()
    app.router.add_get("/", web_landing)
    app.router.add_get("/api/{sid}/state", web_state)
    app.router.add_post("/api/{sid}/control", web_control)
    app.router.add_post("/api/{sid}/theme", web_theme)
    app.router.add_post("/api/{sid}/theme/logo", web_theme_logo)
    app.router.add_delete("/api/{sid}/theme/logo", web_theme_logo)
    app.router.add_get("/media/{sid}/{track_id}", web_media)
    app.router.add_get("/theme-logo/{sid}", web_theme_logo_file)
    app.router.add_get("/{sid}", web_page)
    return app


web_runner = None


async def start_web_server():
    """Start the web server in the bot's event loop (once)."""
    global web_runner
    if web_runner is not None:
        return
    runner = web.AppRunner(build_web_app(), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    except OSError as e:
        # A busy port must not keep the music bot from starting.
        print(f"❌ Web server could not listen on port {WEB_PORT}: {e}")
        await runner.cleanup()
        return
    web_runner = runner
    print(f"Web server listening on port {WEB_PORT} ({PUBLIC_BASE_URL})")


async def setup_hook():
    await start_web_server()
    task = asyncio.get_running_loop().create_task(session_cleanup_loop())
    _background_tasks.add(task)


bot.setup_hook = setup_hook


def get_discord_token():
    """Get Discord token from environment variables or token file (fallback)"""
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        try:
            with open("token", "r") as f:
                TOKEN = f.read().strip()
            print(
                "⚠️  Warning: Using token file. Consider using DISCORD_TOKEN environment variable for better security."
            )
        except FileNotFoundError:
            print("❌ Error: No Discord token found!")
            print(
                "Please set DISCORD_TOKEN environment variable or create a 'token' file."
            )
            print("Get your token from: https://discord.com/developers/applications")
            exit(1)
    return TOKEN


if __name__ == "__main__":
    print("Starting Discord Music Bot (fixed version)")
    print("-" * 50)
    TOKEN = get_discord_token()
    bot.run(TOKEN)
