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

# Slash commands sync globally at startup (up to an hour to show everywhere).
# SLASH_SYNC_GUILD, a server ID, syncs to that server only, at once: for dev.
_slash_guild = (os.getenv("SLASH_SYNC_GUILD") or "").strip()
if _slash_guild.isdigit():
    SLASH_SYNC_GUILD = int(_slash_guild)
else:
    SLASH_SYNC_GUILD = None
    if _slash_guild:
        print(f"⚠️  SLASH_SYNC_GUILD={_slash_guild!r} is not a server ID; ignoring.")

# Web server for the synced clip page. The public URL is what gets posted in
# Discord; the tunnel in front of it forwards to WEB_PORT.
WEB_PORT = int(os.getenv("WEB_PORT", "47823"))
# Empty PUBLIC_BASE_URL falls back to the local address.
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL") or f"http://localhost:{WEB_PORT}"
).rstrip("/")
WEB_INDEX = os.path.join(ROOT_DIR, "web", "index.html")
WEB_SW = os.path.join(ROOT_DIR, "web", "sw.js")
WEB_ICON_DIR = os.path.join(ROOT_DIR, "web", "icons")
# Reported by /healthz; the Docker image sets it at build time (release tag).
APP_VERSION = os.getenv("APP_VERSION") or "dev"
# GET /metrics (Prometheus text format) is off unless METRICS is truthy.
METRICS_ENABLED = os.getenv("METRICS", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# A web session outlives the bot's stay in voice; it expires after this many
# hours without activity (playback, a command or a web control).
SESSION_IDLE_HOURS = float(os.getenv("SESSION_IDLE_HOURS", "6"))
SESSION_CLEANUP_SECONDS = 600
# When the queue runs out, the bot stays in voice this long (seconds) before
# leaving, so a track added meanwhile plays at once. An empty channel (no
# human left) cuts the wait to EMPTY_CHANNEL_GRACE_SECONDS.
IDLE_DISCONNECT_SECONDS = max(0.0, float(os.getenv("IDLE_DISCONNECT_SECONDS", "300")))
EMPTY_CHANNEL_GRACE_SECONDS = 30.0
# Autoplay ("radio"): default of the per-session setting, off unless AUTOPLAY is
# 1/true/on/yes. When the queue runs out, a related YouTube track plays on.
AUTOPLAY = os.getenv("AUTOPLAY", "0").strip().lower() in ("1", "true", "on", "yes")
AUTOPLAY_MAX_SECONDS = 600  # Longer tracks (mixes, albums, podcasts) are skipped
AUTOPLAY_HISTORY = 50  # Recently played video ids that autoplay never repeats
AUTOPLAY_CANDIDATES = 30  # Entries read from the related "RD<id>" mix
AUTOPLAY_TRIES = 3  # Candidates downloaded before giving up


# Anti-abuse limits on what may be queued (per-guild overrides: `/limits`, see
# limits.py). These are the defaults of guilds that never changed them.
def _int_env(name, default):
    try:
        return max(0, int(os.getenv(name, str(default)).strip()))
    except ValueError:
        print(f"⚠️  {name} is not a whole number; using {default}.")
        return default


MAX_QUEUE_SIZE = max(1, _int_env("MAX_QUEUE_SIZE", 200))  # Tracks waiting
MAX_TRACKS_PER_USER = _int_env("MAX_TRACKS_PER_USER", 0)  # 0 = unlimited
MAX_TRACK_MINUTES = _int_env("MAX_TRACK_MINUTES", 0)  # 0 = unlimited
PLAY_COOLDOWN_SECONDS = _int_env("PLAY_COOLDOWN_SECONDS", 0)  # 0 = none
# Persistent data (survives restarts): per-guild web page themes and logos.
DATA_DIR = os.getenv("DATA_DIR") or "/app/data"
THEMES_FILE = os.path.join(DATA_DIR, "themes.json")
LOGO_DIR = os.path.join(DATA_DIR, "logos")
VOLUME_FILE = os.path.join(DATA_DIR, "volume.json")
# Per-guild vote-to-skip threshold (`/voteskip threshold`), see voteskip.py.
VOTESKIP_FILE = os.path.join(DATA_DIR, "voteskip.json")
LIMITS_FILE = os.path.join(DATA_DIR, "limits.json")
# Per-guild DJ role (`!djrole`), see permissions.py.
PERMISSIONS_FILE = os.path.join(DATA_DIR, "permissions.json")
# 24/7 mode (`!stay`), see stay.py. STAY_247 is the default of guilds that never
# ran the command (they still need a remembered channel to rejoin at startup).
STAY_FILE = os.path.join(DATA_DIR, "stay.json")
STAY_247 = os.getenv("STAY_247", "0").strip().lower() in ("1", "true", "on", "yes")
# A 24/7 bot dropped from voice rejoins once; dropped again within this many
# seconds it gives up and turns 24/7 off (no reconnect loop).
STAY_RETRY_WINDOW = 120.0
# Automatic backups of DATA_DIR into DATA_DIR/backups (see backup.py): every
# BACKUP_INTERVAL_HOURS hours (0 = off), keeping the newest BACKUP_KEEP.
BACKUP_INTERVAL_HOURS = max(0.0, float(os.getenv("BACKUP_INTERVAL_HOURS") or "0"))
BACKUP_KEEP = max(1, int(os.getenv("BACKUP_KEEP") or "7"))
# Who gets the clip page's control link: "everyone" (posted in the status
# panel next to the view-only link) or "dj" (the panel shows only the
# view-only link; DJs get the control link by DM with `!weblink`). A guild
# with a DJ role set is always handled as "dj".
WEB_CONTROL = (os.getenv("WEB_CONTROL") or "everyone").strip().lower()
if WEB_CONTROL not in ("everyone", "dj"):
    print(f"⚠️  WEB_CONTROL={WEB_CONTROL!r} is not everyone|dj; using 'everyone'.")
    WEB_CONTROL = "everyone"
LANGUAGES_FILE = os.path.join(DATA_DIR, "languages.json")
# Listening statistics (stats.py): SQLite, written by one background thread.
STATS_DB = os.path.join(DATA_DIR, "stats.db")
# Saved playlists (playlists.py): SQLite, per guild. Only links and titles.
PLAYLISTS_DB = os.path.join(DATA_DIR, "playlists.db")
MAX_SAVED_PLAYLISTS = 50  # Per guild
MAX_SAVED_TRACKS = 200  # Per playlist
SAVED_PLAYLIST_NAME_MAX = 32
# A track left (next, jump, previous, !stop) before this much actual play
# counts as skipped.
SKIP_SECONDS = 30.0
STATS_CACHE_SECONDS = 15  # The stats route serves a guild's answer this long
# Live viewer count: a page tab counts while it polled within this window.
# The page polls every 2 s; browsers throttle background tabs to about one
# timer per second (one per minute after several minutes hidden).
VIEWER_WINDOW_SECONDS = 30
MAX_VIEWERS_PER_SESSION = 500
# Live updates (GET /api/{sid}/events, Server-Sent Events): a `ping` event
# after SSE_HEARTBEAT_SECONDS of silence keeps proxies from closing an idle
# stream and lets the page notice a dead one (it falls back to polling), at
# most one state per SSE_MIN_INTERVAL (bursts are coalesced), and a state
# diff every SSE_CHECK_SECONDS catches changes nobody notified.
SSE_HEARTBEAT_SECONDS = 10
SSE_MIN_INTERVAL = 0.1
SSE_CHECK_SECONDS = 2
MAX_STREAMS_PER_SESSION = 100
MAX_STREAMS_PER_CLIENT = 10  # Per client IP (see web._client_key)
MAX_STREAMS_TOTAL = 500  # All sessions; past it, pages poll
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
# Language of the bot's messages and the clip page for guilds that never ran
# `!language` (djsnake/locales/{code}.json; i18n.py checks the value).
BOT_LANGUAGE = (os.getenv("BOT_LANGUAGE") or "en").strip().lower()
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
# Titles kept for /history (no files): the most it can show.
HISTORY_LOG_SIZE = 25
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

# Optional Netscape cookies.txt for age-restricted / members-only videos
# (see cookies.py). Use a throwaway Google account: YouTube may ban accounts
# used by bots.
YTDLP_COOKIES = (os.getenv("YTDLP_COOKIES") or "").strip()

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
    "is not currently live",  # Twitch channel offline
    "live event will begin",  # YouTube live not started yet
)
# Live streams (YouTube lives, Twitch channels, internet radios) are never
# downloaded: FFmpeg reads them as they come, with these reconnect options.
# There is no automix analysis (one-pass loudnorm instead), no SponsorBlock,
# no seek and no crossfade. A live item stops after LIVE_MAX_HOURS (0: never),
# and the queue goes on.
LIVE_MAX_HOURS = max(0.0, float(os.getenv("LIVE_MAX_HOURS", "4") or 0))
LIVE_RECONNECT = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
LIVE_META_SECONDS = 30  # A radio's ICY "StreamTitle" is read this often
LIVE_PROBE_TIMEOUT = 8  # Seconds per HTTP request reading a radio link
LIVE_PLAYLIST_MAX_BYTES = 64 * 1024  # .pls / .m3u files larger are refused
LIVE_ICY_MAX_METAINT = 256 * 1024  # Audio bytes read before ICY metadata
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
# Per-server volume, in percent of the normalised level (100 = unchanged).
VOLUME_DEFAULT = 100
VOLUME_MAX = 150
VOLUME_RAMP_FRAMES = 5  # A volume change glides over this many frames (100 ms)
# Audio filters (per session, see filters.py): presets and the 3-band EQ.
FILTER_PRESETS = (
    "off",
    "bassboost",
    "nightcore",
    "vaporwave",
    "8d",
    "karaoke",
    "soft",
    "eq",
)
EQ_BANDS = ("low", "mid", "high")
EQ_MAX_DB = 12  # Each band from -EQ_MAX_DB to +EQ_MAX_DB dB

# Spotify links: the track list is read from the public embed page (no
# configuration, at most 100 tracks), or from the Web API when both
# credentials are set (client credentials flow, full lists). Each track is then
# searched on YouTube; nothing is ever downloaded from Spotify.
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
SPOTIFY_TIMEOUT = 10  # Seconds per HTTP request
SPOTIFY_MAX_BYTES = 4 * 1024 * 1024  # Larger responses are refused
MAX_SEARCH_LENGTH = 200  # Characters of a plain text search
YOUTUBE_SEARCH_RESULTS = 5  # Candidates compared per Spotify track

# Synced lyrics on the clip page, from LRCLIB (lrclib.net, free, no key).
# Fetched in the background when a track is queued; LYRICS=0 turns them off.
LYRICS_ENABLED = os.getenv("LYRICS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
LYRICS_DIR = os.path.join(DATA_DIR, "lyrics")  # Found lyrics, by video id
LYRICS_TIMEOUT = 6  # Seconds per LRCLIB request
LYRICS_MAX_BYTES = 1024 * 1024  # Larger responses are refused
LYRICS_WAIT_SECONDS = 4.0  # The web route waits this long for a fetch

# Optional Last.fm scrobbling: on only when both the API key and secret are set.
# Each user links their own account with `/lastfm link`; session keys live in
# DATA_DIR/lastfm.json (mode 0600).
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "").strip()
LASTFM_API_SECRET = os.getenv("LASTFM_API_SECRET", "").strip()
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_AUTH_URL = "https://www.last.fm/api/auth/"
LASTFM_TIMEOUT = 8  # Seconds per request
LASTFM_FILE = os.path.join(DATA_DIR, "lastfm.json")
LASTFM_BATCH = 50  # Scrobbles per track.scrobble call (Last.fm's maximum)
LASTFM_MAX_PENDING = 500  # Scrobbles kept per user while Last.fm is down
LASTFM_MIN_TRACK_SECONDS = 30  # Shorter tracks are never scrobbled
LASTFM_MAX_THRESHOLD = 240  # A track counts after half its length or 4 minutes
LASTFM_TOKEN_MINUTES = 55  # A Last.fm auth token is valid for an hour

# Only create a temp directory if one wasn't provided. Using tempfile.mkdtemp()
# directly as the default argument would create an orphan directory on every run
# because default arguments are evaluated eagerly.
TEMP_DIR = os.getenv("TEMP_DIR") or tempfile.mkdtemp()


def _percent_env(name, default):
    """An integer percent 1-100 from the environment, else `default`."""
    try:
        value = int(os.getenv(name, "").strip())
    except ValueError:
        return default
    return value if 1 <= value <= 100 else default


# Vote to skip: more than this percent of the listeners (humans in the voice
# channel who are not deafened) must vote to skip. Default of guilds that
# never ran `/voteskip threshold`.
VOTESKIP_PERCENT = _percent_env("VOTESKIP_PERCENT", 50)
