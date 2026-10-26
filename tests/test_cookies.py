"""YTDLP_COOKIES: validation, private copy, option wiring and error mapping."""

import os
import stat
from unittest import mock

import pytest

from djsnake import config, cookies, media, playback

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tsecret-value\n"
)


@pytest.fixture(autouse=True)
def reset_cookies(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_COOKIES", "")
    cookies._state.update(ready=False, path=None)
    yield
    path = cookies._state["path"]
    if path and os.path.exists(path):
        os.remove(path)
    cookies._state.update(ready=False, path=None)


def write(tmp_path, text, name="cookies.txt"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_validate(tmp_path):
    assert cookies.validate(write(tmp_path, NETSCAPE)) is None
    row = ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n"
    assert cookies.validate(write(tmp_path, row, "b.txt")) is None
    assert "not found" in cookies.validate(str(tmp_path / "missing"))
    assert "empty" in cookies.validate(write(tmp_path, "\n", "c.txt"))
    assert "Netscape" in cookies.validate(write(tmp_path, "{json}", "d.txt"))


def test_unset_means_no_options():
    assert cookies.options() == {}
    assert not cookies.enabled()


def test_setup_copies_to_private_file(tmp_path, monkeypatch):
    source = write(tmp_path, NETSCAPE)
    os.chmod(source, 0o444)
    monkeypatch.setattr(config, "YTDLP_COOKIES", source)
    copy = cookies.setup()
    assert copy and copy != source
    assert stat.S_IMODE(os.stat(copy).st_mode) == 0o600
    with open(copy) as f:
        assert f.read() == NETSCAPE
    assert cookies.options() == {"cookiefile": copy}
    with open(copy, "a") as f:  # yt-dlp writes back to the copy only
        f.write("# changed\n")
    with open(source) as f:
        assert f.read() == NETSCAPE


def test_invalid_file_warns_without_leaking(tmp_path, monkeypatch, capsys):
    source = write(tmp_path, "secret-value but not cookies")
    monkeypatch.setattr(config, "YTDLP_COOKIES", source)
    assert cookies.setup() is None
    out = capsys.readouterr().out
    assert "YTDLP_COOKIES" in out and "secret-value" not in out
    assert cookies.options() == {}


def test_missing_file_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "YTDLP_COOKIES", str(tmp_path / "nope.txt"))
    assert cookies.setup() is None
    assert "ignored" in capsys.readouterr().out


def captured_opts(run):
    with mock.patch.object(media.yt_dlp, "YoutubeDL") as ydl:
        ydl.return_value.__enter__.return_value.extract_info.return_value = {
            "entries": []
        }
        try:
            run()
        except Exception:
            pass  # Only the options matter
        return ydl.call_args[0][0]


CALLS = {
    "playlist": lambda: media.get_playlist_info("https://www.youtube.com/playlist"),
    "search": lambda: media.search_youtube("daft punk", 3),
    "related": lambda: media.related_tracks("dQw4w9WgXcQ"),
    "download": lambda: media._download_once("https://www.youtube.com/watch?v=x"),
}


@pytest.mark.parametrize("name", CALLS)
def test_cookiefile_reaches_every_call(name, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "YTDLP_COOKIES", write(tmp_path, NETSCAPE))
    opts = captured_opts(CALLS[name])
    assert opts["cookiefile"] == cookies.cookie_path()


@pytest.mark.parametrize("name", CALLS)
def test_no_cookiefile_when_unset(name):
    assert "cookiefile" not in captured_opts(CALLS[name])


@pytest.mark.parametrize(
    "message, kind",
    [
        ("Sign in to confirm your age. This video may be inappropriate", "age"),
        ("ERROR: [youtube] x: This video is age-restricted", "age"),
        ("Join this channel to get access to members-only content", "members"),
        ("Private video. Sign in if you've been granted access", "private"),
        ("Sign in to confirm you’re not a bot", None),
        ("Sign in required", "signin"),
        ("HTTP Error 403: Forbidden", None),
    ],
)
def test_classify_error(message, kind):
    assert media.classify_error(message) == kind


def test_error_text_suggests_cookies_only_when_not_configured(tmp_path, monkeypatch):
    err = RuntimeError("Error during download: Sign in to confirm your age")
    text = playback.download_error_text(err, "en")
    assert "age-restricted" in text and "YTDLP_COOKIES" in text
    assert "restriction" in playback.download_error_text(err, "fr")
    monkeypatch.setattr(config, "YTDLP_COOKIES", write(tmp_path, NETSCAPE))
    cookies.setup()
    assert "YTDLP_COOKIES" not in playback.download_error_text(err, "en")


def test_generic_error_unchanged():
    text = playback.download_error_text(RuntimeError("boom"), "en")
    assert "Download error" in text and "boom" in text


def test_failed_copy_leaves_no_temp_file(tmp_path, monkeypatch):
    src = write(tmp_path, NETSCAPE)
    made = []
    real_mkstemp = cookies.tempfile.mkstemp

    def mkstemp(**kwargs):
        fd, path = real_mkstemp(dir=str(tmp_path), **kwargs)
        made.append(path)
        return fd, path

    def broken_copy(src_file, dst_file):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cookies.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(cookies.shutil, "copyfileobj", broken_copy)
    assert cookies.setup(src) is None
    assert made and not os.path.exists(made[0])


def test_copy_is_deleted_on_cleanup_and_resetup(tmp_path):
    src = write(tmp_path, NETSCAPE)
    first = cookies.setup(src)
    second = cookies.setup(src)
    assert not os.path.exists(first) and os.path.exists(second)
    cookies.cleanup()
    assert not os.path.exists(second) and cookies._state["path"] is None


def test_cleanup_runs_at_exit():
    import atexit

    with mock.patch.object(atexit, "register") as register:
        import importlib

        importlib.reload(cookies)
    register.assert_called_once_with(cookies.cleanup)


def test_cookie_files_are_ignored_by_git_and_docker():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in (".gitignore", ".dockerignore"):
        with open(os.path.join(root, name)) as f:
            lines = f.read().splitlines()
        assert "cookies.txt" in lines and "*.cookies.txt" in lines, name
