"""Automix analysis parsing, FFmpeg playback options and URL validation."""

import pytest

from djsnake import media
from djsnake.commands import parse_position
from djsnake.media import (
    _parse_analysis,
    _remove_offtopic,
    crossfade_eligible,
    parse_youtube_url,
    playback_options,
)

LOUDNORM_OUTPUT = (
    "[Parsed_loudnorm_1 @ 0x1] \n{\n"
    '\t"input_i" : "-8.23",\n'
    '\t"input_tp" : "2.23",\n'
    '\t"input_lra" : "8.30",\n'
    '\t"input_thresh" : "-18.41",\n'
    '\t"output_i" : "-14.1",\n'
    '\t"target_offset" : "-1.25"\n}\n'
)
DURATION_LINE = "  Duration: 00:03:20.00, start: 0.000000, bitrate: 900 kb/s\n"
SILENCES = (
    "[silencedetect @ 0x2] silence_start: 0\n"
    "[silencedetect @ 0x2] silence_end: 2.5 | silence_duration: 2.5\n"
    "[silencedetect @ 0x2] silence_start: 100\n"
    "[silencedetect @ 0x2] silence_end: 101 | silence_duration: 1\n"
    "[silencedetect @ 0x2] silence_start: 195.25\n"
    "[silencedetect @ 0x2] silence_end: 200 | silence_duration: 4.75\n"
)
MEASURED = {
    "input_i": -8.23,
    "input_tp": 2.23,
    "input_lra": 8.3,
    "input_thresh": -18.41,
    "target_offset": -1.25,
}
ANALYSED = {
    "filepath": "x",
    "duration": 200.0,
    "start": 2.5,
    "end": 195.25,
    "loudnorm": MEASURED,
}


# --- _parse_analysis ---------------------------------------------------------


def test_parse_analysis_reads_window_and_duration():
    a = _parse_analysis(DURATION_LINE + SILENCES + LOUDNORM_OUTPUT)
    assert (a["duration"], a["start"], a["end"]) == (200.0, 2.5, 195.25)


def test_parse_analysis_reads_loudnorm():
    a = _parse_analysis(DURATION_LINE + SILENCES + LOUDNORM_OUTPUT)
    assert a["loudnorm"] == MEASURED


def test_parse_analysis_caps_leading_trim_and_open_trailing_silence():
    a = _parse_analysis(
        DURATION_LINE + "silence_start: 0\nsilence_end: 45 | x\nsilence_start: 150\n"
    )
    assert (a["start"], a["end"]) == (20.0, 150.0)


def test_parse_analysis_mostly_silent_or_infinite_loudness_gives_defaults():
    a = _parse_analysis(
        DURATION_LINE
        + "silence_start: 0\nsilence_end: 5 | x\nsilence_start: 12\n"
        + LOUDNORM_OUTPUT.replace('"-8.23"', '"-inf"')
    )
    assert (a["start"], a["end"], a["loudnorm"]) == (0.0, 200.0, None)


def test_parse_analysis_empty_output_gives_defaults():
    assert _parse_analysis("") == {
        "duration": None,
        "start": 0.0,
        "end": None,
        "loudnorm": None,
    }


# --- playback_options ----------------------------------------------------------


def test_unanalysed_track_keeps_plain_options():
    legacy = {"filepath": "x", "duration": 100.0}
    assert playback_options(legacy, 0) == (0, {"options": "-vn"})
    assert playback_options(legacy, 30)[1] == {
        "options": "-vn",
        "before_options": "-ss 30.000",
    }


def test_play_from_start_normalises_and_fades():
    offset, kw = playback_options(ANALYSED, 0)
    assert offset == 2.5
    assert kw["before_options"] == "-ss 2.500 -t 192.750"
    assert kw["options"].startswith("-vn -af loudnorm=")
    assert "measured_I=-8.23" in kw["options"]
    assert "linear=true" in kw["options"]
    assert "afade=t=in:st=0:d=1.000" in kw["options"]
    assert kw["options"].endswith("afade=t=out:st=188.750:d=4.000")


def test_seek_keeps_fade_out_at_the_end():
    offset, kw = playback_options(ANALYSED, 100)
    assert offset == 100
    assert kw["before_options"] == "-ss 100.000 -t 95.250"
    assert "afade=t=in:st=0:d=0.300" in kw["options"]
    assert kw["options"].endswith("afade=t=out:st=91.250:d=4.000")


def test_seek_into_the_fade_out_fades_what_is_left():
    _, kw = playback_options(ANALYSED, 193)
    assert kw["before_options"] == "-ss 193.000 -t 2.250"
    assert kw["options"].endswith("afade=t=out:st=0.000:d=2.250")


def test_seek_past_the_end_is_clamped_to_end_minus_one():
    offset, kw = playback_options(ANALYSED, 1000)
    assert offset == 194.25
    assert "-t 1.000" in kw["before_options"]


def test_no_loudnorm_filter_when_not_measured():
    _, kw = playback_options(dict(ANALYSED, loudnorm=None), 0)
    assert "loudnorm" not in kw["options"]
    assert "afade" in kw["options"]


@pytest.mark.parametrize(
    "track, eligible",
    [
        (None, False),
        ({"duration": 100.0}, False),  # Not analysed
        ({"start": 0.0, "end": 14.9}, False),
        ({"start": 5.0, "end": 20.0}, True),
        ({"start": None, "end": 15.0}, True),
    ],
)
def test_crossfade_eligible(track, eligible):
    assert crossfade_eligible(track) is eligible


# --- SponsorBlock cut ----------------------------------------------------------


def test_sponsorblock_failure_is_best_effort(monkeypatch):
    class Unreachable:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, info):
            raise RuntimeError("Unable to communicate with SponsorBlock API")

    monkeypatch.setattr(media, "SponsorBlockPP", Unreachable)
    _remove_offtopic(None, {"id": "x"}, "/nonexistent.mp4")  # Must not raise


# --- URL validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz",
            ("single", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ",
            ("single", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ),
        (
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            ("single", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ),
        (
            "https://music.youtube.com/playlist?list=PLabc",
            ("playlist", "https://www.youtube.com/playlist?list=PLabc"),
        ),
    ],
)
def test_parse_youtube_url_rebuilds_canonical_links(url, expected):
    assert parse_youtube_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com@evil.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com:8080/watch?v=dQw4w9WgXcQ",
        "http://127.0.0.1:4416/",
        "file:///etc/passwd",
        "https://www.youtube.com/channel/UC123",
        "https://www.youtube.com/watch?v=short",
        None,
    ],
)
def test_parse_youtube_url_rejects_other_links(url):
    assert parse_youtube_url(url) is None


@pytest.mark.parametrize(
    "text, seconds",
    [("90", 90.0), ("1:30", 90.0), ("1:02:03", 3723.0), ("0", 0.0)],
)
def test_parse_position_accepts(text, seconds):
    assert parse_position(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "-5", "1:2:3:4", "inf", "nan"])
def test_parse_position_rejects(text):
    assert parse_position(text) is None
