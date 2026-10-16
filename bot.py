import os
import asyncio
import json
import re
import secrets
import time
import types
import yt_dlp
import discord
from discord.ext import commands
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
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://dj.example.com").rstrip("/")
WEB_INDEX = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "web", "index.html"
)
# A web session outlives the bot's stay in voice; it expires after this many
# hours without activity (playback, a command or a web control).
SESSION_IDLE_HOURS = float(os.getenv("SESSION_IDLE_HOURS", "6"))
SESSION_CLEANUP_SECONDS = 600
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
        # "next", "previous" or "restart". Set by controls before stop().
        self.pending_action = None
        # Bumped on each track start; a stale `after` callback is ignored.
        self.play_gen = 0

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


async def open_session(ctx, player, announce=True):
    """Reuse the guild's live session or start one, and post its link."""
    touch(player)
    created = False
    if not player.sid:
        player.sid = secrets.token_urlsafe(16)
        sessions[player.sid] = ctx.guild.id
        created = True
        print(f"Web session opened for {ctx.guild.name}: {player.sid}")
    if not (created or announce):
        return
    player.text_channel = ctx.channel
    try:
        await ctx.channel.send(
            f"📺 Watch the clip live: {PUBLIC_BASE_URL}/{player.sid}"
        )
    except Exception as e:
        print(f"Could not post session link: {e}")


async def announce(player, text):
    """Post a short notice in the session's text channel."""
    if player.text_channel is None:
        return
    try:
        await player.text_channel.send(text)
    except Exception as e:
        print(f"Could not announce: {e}")


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

        video_id = info.get("id", "")
        duration = info.get("duration")

        def track(filepath):
            return {
                "id": secrets.token_hex(4),
                "filepath": filepath,
                "title": info.get("title", "Unknown"),
                "duration": float(duration) if duration else None,
                "thumbnail": info.get("thumbnail"),
                "url": info.get("webpage_url") or url,
            }

        # yt-dlp reports the final (merged) path
        for download in info.get("requested_downloads") or []:
            filepath = download.get("filepath")
            if filepath and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                return track(filepath)

        # Fallback: find the downloaded file
        temp_files = os.listdir(temp_dir)

        for f in temp_files:
            if video_id in f:
                filepath = os.path.join(temp_dir, f)
                # Verify file exists and has content
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    return track(filepath)

        # If not found by ID, take the most recent
        if temp_files:
            latest_file = max(
                temp_files,
                key=lambda f: os.path.getctime(os.path.join(temp_dir, f)),
            )
            filepath = os.path.join(temp_dir, latest_file)
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                return track(filepath)

        raise RuntimeError("File not found after download")


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
    ctx, player, url, skip_voice_check=False, from_web=False
):
    """Download one track and queue it. Returns the track, or None."""
    epoch = player.epoch
    try:
        if _cancelled(player, epoch):
            return None

        if not skip_voice_check:
            if not player.voice_client or not player.voice_client.is_connected():
                if not await join_voice_channel(ctx):
                    await ctx.send("❌ Unable to join voice channel!")
                    return None

        status_message = await ctx.send("⏳ Downloading...")
        loop = asyncio.get_running_loop()
        track = await loop.run_in_executor(None, download_media, url)

        if _cancelled(player, epoch):
            discard_tracks([track])
            await status_message.delete()
            return None

        player.add_to_queue(track)
        if from_web:
            text = f"➕ Added from the web page: **{track['title']}**"
        else:
            text = f"✅ **{track['title']}** added to queue."
        await status_message.edit(content=text)

        if not player.is_playing:
            await play_next(ctx)
        return track

    except Exception as e:
        await ctx.send(f"❌ Download error: {str(e)}")
        if "status_message" in locals():
            try:
                await status_message.delete()
            except:
                pass
        return None


async def queue_playlist(ctx, player, entries, from_web=False):
    """Download playlist entries one by one into the queue while earlier
    ones play. `loading` keeps play_next from leaving voice meanwhile."""
    epoch = player.epoch
    player.loading_jobs += 1
    stopped = False
    try:
        for i, entry in enumerate(entries, 1):
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

            title = entry.get("title", "Unknown title")
            await ctx.send(f"⏳ [{i}/{len(entries)}] Adding **{title}**...")

            await download_and_queue_song(
                ctx, player, video_url, skip_voice_check=True, from_web=from_web
            )
    finally:
        player.loading_jobs = max(0, player.loading_jobs - 1)

    if stopped or _cancelled(player, epoch):
        await ctx.send("⏹️ Download stopped.")
        return
    await ctx.send("✅ All playlist tracks have been added!")
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
    finished = player.now_playing
    player.now_playing = None
    player.started_at = None
    player.paused_at = None
    player.paused_total = 0.0

    if action == "restart" and finished:
        next_track = finished
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
        await ctx.send("🎵 Queue is empty!")
        return

    # Check voice connection
    if not player.voice_client or not player.voice_client.is_connected():
        if not await join_voice_channel(ctx):
            player.queue.insert(0, next_track)
            player.is_playing = False
            await ctx.send("❌ Unable to join voice channel!")
            return

    player.play_gen += 1
    gen = player.play_gen
    loop = asyncio.get_running_loop()

    def after_playing(error):
        # Runs on the audio thread: only hand over to the event loop.
        if error:
            print(f"Playback error: {error}")
        try:
            asyncio.run_coroutine_threadsafe(play_next(ctx, gen, player), loop)
        except Exception as e:
            print(f"Error scheduling next song: {e}")

    player.now_playing = next_track
    player.is_playing = True
    # The file carries video for the web page; Discord only needs audio.
    audio_source = discord.FFmpegPCMAudio(next_track["filepath"], options="-vn")
    try:
        player.voice_client.play(audio_source, after=after_playing)
    except Exception:
        player.now_playing = None
        player.is_playing = False
        discard_tracks([next_track])
        raise
    player.started_at = time.monotonic()
    player.last_active = _clock()
    # The track already plays: a failed announcement must not mark the
    # player idle (play_next would then try to start a second track).
    try:
        if next_track is finished:
            await ctx.send(f"🔁 Restarting: **{next_track['title']}**")
        else:
            await ctx.send(f"🎵 Now playing: **{next_track['title']}**")
    except Exception as e:
        print(f"Could not announce track: {e}")


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
    if elapsed > PREVIOUS_RESTART_SECONDS or not player.history:
        action = "restart"
    else:
        action = "previous"
    await _stop_with(player, action)
    return action


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
                await ctx.send(f"📋 Adding {len(entries)} tracks from the web page...")
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
        wait_msg = await ctx.send("⏳ Analyzing source...")

        # Try to extract as playlist
        loop = asyncio.get_running_loop()
        playlist_entries = await loop.run_in_executor(None, get_playlist_info, url)

        if playlist_entries:
            # It's a playlist
            await wait_msg.delete()

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
            await ctx.send(f"📋 Adding {len(playlist_entries)} tracks to queue...")
            await queue_playlist(ctx, player, playlist_entries)
        else:
            # It's a single video
            await wait_msg.delete()
            if not player.should_stop:
                await download_and_queue_song(ctx, player, url)

    except Exception as e:
        await ctx.send(f"❌ An error occurred: {str(e)}")
        if "wait_msg" in locals():
            try:
                await wait_msg.delete()
            except:
                pass


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
    await ctx.send("⏹️ Music stopped and queue cleared.")


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


async def _run_command_control(ctx, control):
    """Run a control for a Discord command; returns its result or None."""
    player = music_players.get(ctx.guild.id)
    if not player:
        await ctx.send("❌ No music is currently playing.")
        return None
    try:
        return await control(player)
    except ControlError as e:
        await ctx.send(f"❌ {e.english}")
        return None


@bot.command(name="next")
async def next_track(ctx):
    player = music_players.get(ctx.guild.id)
    if player and player.in_voice() and player.is_playing:
        await ctx.send("⏭️ Skipping...")
    # Stopping triggers the after_playing callback, which plays the next track.
    await _run_command_control(ctx, control_next)


@bot.command(name="pause")
async def pause(ctx):
    changed = await _run_command_control(ctx, control_pause)
    if changed is not None:
        await ctx.send("⏸️ Paused." if changed else "⏸️ Already paused.")


@bot.command(name="resume")
async def resume(ctx):
    changed = await _run_command_control(ctx, control_resume)
    if changed is not None:
        await ctx.send("▶️ Resumed." if changed else "▶️ Not paused.")


@bot.event
async def on_command_error(ctx, error):
    """Global command error handler to avoid ugly tracebacks."""
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Try `{COMMAND_PREFIX}help`.")
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
#   POST /api/{sid}/control    {"action", "id"?, "index"?, "url"?} -> {"ok": ...}
#   GET /media/{sid}/{id}      the mp4 of a playing, queued or history track
# Paths are never built from the request: tracks are looked up by id.


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
    return web.FileResponse(WEB_INDEX, headers={"Cache-Control": "no-cache"})


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
            now_playing["elapsed"] = player.elapsed()
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
            "server_time": time.time(),
            "now_playing": now_playing,
            "queue": queue,
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


async def web_control(request):
    sid = request.match_info["sid"]
    player = _session_player(sid)
    if player is None:
        return _control_error(404, "Session inconnue ou expirée.")
    guild_id = sessions[sid]

    if (request.content_length or 0) > MAX_CONTROL_BODY:
        return _control_error(400, "Requête trop volumineuse.")
    # StreamReader.read() may return a partial chunk: read until EOF.
    body = b""
    while len(body) <= MAX_CONTROL_BODY:
        chunk = await request.content.read(MAX_CONTROL_BODY + 1 - len(body))
        if not chunk:
            break
        body += chunk
    if len(body) > MAX_CONTROL_BODY:
        return _control_error(400, "Requête trop volumineuse.")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return _control_error(400, "Requête invalide.")
    if not isinstance(data, dict):
        return _control_error(400, "Requête invalide.")
    action = data.get("action")
    if action not in ("next", "previous", "pause", "resume", "remove", "move", "add"):
        return _control_error(400, "Action inconnue.")

    track_id = data.get("id")
    index = data.get("index")
    if action in ("remove", "move") and not (
        isinstance(track_id, str) and 0 < len(track_id) <= 64
    ):
        return _control_error(400, "Morceau invalide.")
    if action == "move" and (type(index) is not int):
        return _control_error(400, "Position invalide.")
    if action == "add" and not isinstance(data.get("url"), str):
        return _control_error(400, "Lien manquant.")

    touch(player)
    if player.sid != sid:  # Expired just now
        return _control_error(404, "Session inconnue ou expirée.")

    try:
        if action == "next":
            await control_next(player)
            await announce(player, "⏭️ Skipped from the web page")
        elif action == "previous":
            if await control_previous(player) == "restart":
                await announce(player, "⏮️ Restarted from the web page")
            else:
                await announce(player, "⏮️ Previous track from the web page")
        elif action == "pause":
            if await control_pause(player):
                await announce(player, "⏸️ Paused from the web page")
        elif action == "resume":
            if await control_resume(player):
                await announce(player, "▶️ Resumed from the web page")
        elif action == "remove":
            track = await control_remove(player, track_id)
            await announce(
                player, f"🗑️ Removed from the web page: **{track['title']}**"
            )
        elif action == "move":
            track = await control_move(player, track_id, index)
            await announce(
                player,
                f"↕️ Moved from the web page: **{track['title']}** "
                f"(now #{index + 1})",
            )
        elif action == "add":
            control_add(player, guild_id, data["url"])
            return web.json_response({"ok": True}, status=202)
    except ControlError as e:
        return _control_error(e.status, e.french)
    return web.json_response({"ok": True})


def build_web_app():
    app = web.Application()
    app.router.add_get("/", web_landing)
    app.router.add_get("/api/{sid}/state", web_state)
    app.router.add_post("/api/{sid}/control", web_control)
    app.router.add_get("/media/{sid}/{track_id}", web_media)
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
