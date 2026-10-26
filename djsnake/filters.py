"""Audio filters: the FFmpeg `-af` chain and playback rate of each preset.

A session has one filter (`player.filter`, one of FILTER_PRESETS) and, for
the `eq` preset, three band gains (`player.eq`, dB). The chain is appended
after the automix filters (loudnorm and the fades, see
media.playback_options()), so the fades are still timed in file time; the
speed presets come last and play the whole result faster or slower.

`rate()` is how fast file time runs against the wall clock: 1.25 means 25 s
of the file play in 20 s. Elapsed, seeks and the state's start/end stay in
file time; only frame counts (mixer) and wall durations are scaled.
"""

from .config import EQ_BANDS, EQ_MAX_DB, FILTER_PRESETS

# Every non-empty chain starts here: a known sample rate (loudnorm outputs
# 192 kHz, and asetrate needs to know the input rate) and two channels (pan
# and apulsator need a stereo input, some files are mono).
_PREFIX = ["aresample=48000", "aformat=channel_layouts=stereo"]
# Keeps boosts from clipping when the s16 output is written.
_LIMITER = "alimiter=limit=0.9:level=false"

# Speed presets: asetrate replays the 48 kHz samples at another rate (speed
# and pitch move together, like a record), aresample brings it back to 48 kHz.
RATES = {"nightcore": 1.25, "vaporwave": 0.8}

_PRESETS = {
    "off": [],
    "bassboost": ["volume=-4dB", "bass=g=10:f=90:w=0.7", _LIMITER],
    "nightcore": ["asetrate=60000", "aresample=48000"],
    "vaporwave": ["asetrate=38400", "aresample=48000", "lowpass=f=9000"],
    # A slow auto-pan: the sound circles from one ear to the other (8 s).
    "8d": ["apulsator=hz=0.125:amount=0.85"],
    # Centre removal above 150 Hz (where the lead vocal usually sits); the
    # bass and kick stay, summed to mono.
    "karaoke": [
        "asplit=2[kl][kh];"
        "[kl]lowpass=f=150,pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1[klo];"
        "[kh]highpass=f=150,pan=stereo|c0=c0-c1|c1=c1-c0[khi];"
        "[klo][khi]amix=inputs=2:normalize=0"
    ],
    "soft": ["lowpass=f=3200", "volume=1dB"],
    "eq": [],  # Built from the band gains, see eq_filters()
}

DEFAULT_EQ = {band: 0 for band in EQ_BANDS}


def parse_filter(text):
    """The preset named by `text` (case and a few aliases tolerated), or None."""
    name = str(text or "").strip().lower()
    name = {"none": "off", "bass": "bassboost"}.get(name, name)
    return name if name in FILTER_PRESETS else None


def valid_gain(value):
    """An int (not a bool) from -EQ_MAX_DB to EQ_MAX_DB."""
    return type(value) is int and -EQ_MAX_DB <= value <= EQ_MAX_DB


def clean_eq(eq):
    """{low, mid, high} from a dict of band gains; None if any given band is
    not a valid gain or the dict has other keys. Missing bands are 0."""
    if not isinstance(eq, dict) or set(eq) - set(EQ_BANDS):
        return None
    out = dict(DEFAULT_EQ)
    for band, value in eq.items():
        if not valid_gain(value):
            return None
        out[band] = value
    return out


def eq_filters(eq):
    gains = clean_eq(eq or {}) or DEFAULT_EQ
    filters = []
    if gains["low"]:
        filters.append(f"bass=g={gains['low']}:f=100:w=0.7")
    if gains["mid"]:
        filters.append(f"equalizer=f=1000:t=o:w=1.5:g={gains['mid']}")
    if gains["high"]:
        filters.append(f"treble=g={gains['high']}:f=5000:w=0.7")
    if any(v > 0 for v in gains.values()):
        filters.append(_LIMITER)
    return filters


def filter_list(name, eq=None):
    """The FFmpeg filters of preset `name` (an empty list: nothing to add)."""
    filters = eq_filters(eq) if name == "eq" else list(_PRESETS.get(name, []))
    return _PREFIX + filters if filters else []


def chain(name, eq=None):
    """The preset as one `-af` fragment ("" for none)."""
    return ",".join(filter_list(name, eq))


def rate(name):
    """Playback speed of preset `name` (file seconds per wall second)."""
    return RATES.get(name, 1.0)
