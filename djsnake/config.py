"""Configuration: environment variables and constants, read once at import."""

import os
import tempfile

import discord
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Repository root: web/ (the clip page) lives next to the package.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
WEB_INDEX = os.path.join(ROOT_DIR, "web", "index.html")
# A web session outlives the bot's stay in voice; it expires after this many
# hours without activity (playback, a command or a web control).
SESSION_IDLE_HOURS = float(os.getenv("SESSION_IDLE_HOURS", "6"))
SESSION_CLEANUP_SECONDS = 600
# When the queue runs out, the bot stays in voice this long (seconds) before
# leaving, so a track added meanwhile plays at once. An empty channel (no
# human left) cuts the wait to EMPTY_CHANNEL_GRACE_SECONDS.
IDLE_DISCONNECT_SECONDS = max(0.0, float(os.getenv("IDLE_DISCONNECT_SECONDS", "300")))
EMPTY_CHANNEL_GRACE_SECONDS = 30.0
# Persistent data (survives restarts): per-guild web page themes and logos.
DATA_DIR = os.getenv("DATA_DIR") or "/app/data"
THEMES_FILE = os.path.join(DATA_DIR, "themes.json")
LOGO_DIR = os.path.join(DATA_DIR, "logos")
# Listening statistics (stats.py): SQLite, written by one background thread.
STATS_DB = os.path.join(DATA_DIR, "stats.db")
# A track left (next, jump, previous, !stop) before this much actual play
# counts as skipped.
SKIP_SECONDS = 30.0
STATS_CACHE_SECONDS = 15  # The stats route serves a guild's answer this long
# Live viewer count: a page tab counts while it polled within this window.
# The page polls every 2 s; browsers throttle background tabs to about one
# timer per second (one per minute after several minutes hidden).
VIEWER_WINDOW_SECONDS = 30
MAX_VIEWERS_PER_SESSION = 500
# Clip page themes. "custom" is a custom palette on top of a preset's shapes.
THEME_PRESETS = (
    "arcade",
    "minimal",
    "night",
    "vinyl",
    "club",
    "terminal",
    "space",
    "cobalt",
)
THEME_SKINS = THEME_PRESETS + ("custom",)
# Fallback theme for guilds that never chose one.
WEB_THEME = (os.getenv("WEB_THEME") or "arcade").strip().lower()
if WEB_THEME not in THEME_PRESETS:
    print(f"⚠️  WEB_THEME={WEB_THEME!r} is not a preset; using 'arcade'.")
    WEB_THEME = "arcade"
CUSTOM_DEFAULTS = {
    "base": "night",
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
LOOP_MODES = ("off", "track", "queue")

CROSSFADE_SECONDS = 6.0
CROSSFADE_MIN_TRACK = 15.0
FRAMES_PER_SECOND = 50  # discord.py reads 20 ms PCM frames
PCM_FRAME_SIZE = discord.opus.Encoder.FRAME_SIZE  # 3840 bytes, 48 kHz s16 stereo

# Spotify links: the track list is read from the public embed page (no
# configuration, at most 100 tracks), or from the Web API when both
# credentials are set (client credentials flow, full lists). Each track is then
# searched on YouTube; nothing is ever downloaded from Spotify.
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
SPOTIFY_TIMEOUT = 10  # Seconds per HTTP request
SPOTIFY_MAX_BYTES = 4 * 1024 * 1024  # Larger responses are refused
YOUTUBE_SEARCH_RESULTS = 5  # Candidates compared per Spotify track

# Only create a temp directory if one wasn't provided. Using tempfile.mkdtemp()
# directly as the default argument would create an orphan directory on every run
# because default arguments are evaluated eagerly.
TEMP_DIR = os.getenv("TEMP_DIR") or tempfile.mkdtemp()
