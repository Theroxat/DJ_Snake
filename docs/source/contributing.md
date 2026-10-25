# Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/Theroxat/DJ_Snake). For how the code is organised, read the [architecture overview](architecture.md). The bot lives in the `djsnake/` package; `bot.py` is the entry point.

## Development setup

```bash
git clone https://github.com/Theroxat/DJ_Snake.git && cd DJ_Snake
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # set DISCORD_TOKEN and POT_BASE_URL
```

You need `ffmpeg` and `deno` on your `PATH`.

## Checks

CI runs these on Python 3.10 to 3.15, and a change must pass them:

```bash
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
black --check --diff .
python -c "import bot"
pytest -q
```

Black uses its default 88-column limit.

## Tests

Run `pytest` after installing `requirements-dev.txt`. The suite in `tests/` needs no network, Discord or YouTube: voice, channels and FFmpeg are faked, and the few tests that need the real `ffmpeg` binary are skipped without it. Add tests for any behaviour you change.

A plain `pytest` leaves out the slower end-to-end tests (see below).

### End-to-end tests

The tests marked `e2e` (in `tests/e2e/`) run the real pipeline as far as it goes without Discord or the network, and are skipped by a plain `pytest`. They generate short test media with ffmpeg at run time (a sine sweep whose pitch encodes the position, a quiet steady tone, a test-pattern video), then:

- **Audio** (`test_e2e_audio.py`): the automix analysis (silence trim, loudnorm first pass), then the real `play_next()` chain with `discord.FFmpegPCMAudio` and the crossfade mixer, read frame by frame by a fake voice client. They check the frame counts, the trim, the loudness normalisation, the fades, a crossfade overlap, where a seek lands and the volume gain.
- **Web** (`test_e2e_web.py`): the real aiohttp app on a free port with a session playing that media, and `web/index.html` in headless Chromium through Playwright. They check that the page loads, that the video stays in sync with the server clock, live updates, the next, pause, seek and add controls, the view-only link, the theme menu, the lyrics panel (with a fake LRCLIB) and the 320 px layout.

They need `ffmpeg` on your `PATH` and Playwright's Chromium:

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium   # add --with-deps on a bare Linux box
pytest -m e2e                           # about a minute
```

Without ffmpeg, all of them are skipped; without Playwright or its Chromium, only the web ones are. CI runs them in a separate `e2e` job.

### Manual test on a real Discord server

The automated tests cannot reach Discord, so the voice connection, slash command sync and the real downloads still need a hand test before a release. Run the bot against a test server with a second account (or a friend) in the voice channel, keep the clip page open in a browser, and tick the list below. It takes about 20 minutes. Use short tracks.

**Playing**

- [ ] `!play <YouTube video link>` from a voice channel: the bot joins, posts the status message with the clip page links, and you hear the track; the page shows the clip in sync with the sound.
- [ ] `/play query:<YouTube video link>`: same, with a private ✅ instead of a reaction.
- [ ] `!play <YouTube playlist link>`: one "Loading N tracks" message, edited at the end; tracks play while the rest downloads.
- [ ] `!play <radio or mix link>` (a `watch?v=...&list=RD...` link): the one-track-or-whole-list buttons appear and both choices work.
- [ ] `!play daft punk one more time` (plain text): the best YouTube match is queued; nonsense text gives "No YouTube match found".
- [ ] `!play <Spotify track link>` and a Spotify playlist or album: matched on YouTube, with a "not found on YouTube" count if any.
- [ ] `!play <SoundCloud track link>` (and a Bandcamp track if you can): an audio-only track shows its artwork on the page.
- [ ] `!play <Deezer or Apple Music link>`: matched on YouTube like Spotify.
- [ ] `!play https://example.com/x`: refused with "link not accepted".

**Queue and playback commands** (try a few as slash commands too)

- [ ] `!list` shows the queue; `!next` skips; `!pause` and `!resume` pause and resume the sound and the page.
- [ ] `!seek 1:00` jumps there, on Discord and on the page.
- [ ] `!crossfade on`, then let a track end: the next one fades in over it and the page cross-dissolves the clips.
- [ ] `!loop track` replays the track at its end; `!loop queue` sends finished tracks to the end of the queue; `!loop` cycles; `!shuffle` reorders the queue.
- [ ] `!volume` shows 100%; `!volume 40` lowers the sound without restarting it; the page's slider follows; restart the bot and `!volume` still says 40.
- [ ] `!autoplay on` with one track queued: a related track plays after it; `!autoplay off`.
- [ ] Let the queue run out: the status says "Queue empty — leaving in …", and a track added from the page during that time plays at once.
- [ ] `!stop` stops, empties the queue and leaves; `!play` again reuses the same clip page link. `!reset` reconnects cleanly.

**Saved playlists**

- [ ] `!playlist save test`, `!playlist list`, `!playlist show test`, `!stop`, then `!playlist load test` queues the tracks again; `!playlist add test` and `!playlist delete test` work; another member cannot delete your playlist.
- [ ] The page's save and load playlist controls do the same.

**Clip page** (control link)

- [ ] Previous, next, pause, the seek bar and the add box work; a queued track can be played now, removed and dragged to a new place.
- [ ] The crossfade, loop, shuffle, autoplay and volume controls change the session (the status message follows).
- [ ] The Lyrics button shows synced lyrics for a well-known song, the sung line following the sound.
- [ ] The theme menu switches presets and the choice survives a reload; the Stats card opens and shows plays.
- [ ] The page works on a phone (no horizontal scroll) and keeps up after the phone is locked for a minute.

**Permissions and links**

- [ ] The view-only link shows the page with a "View only" badge and no controls.
- [ ] `!djrole <role>` as an admin: the status message now shows only the view-only link, the old control link stops working, a member without the role gets refused on `!next` (but may skip a track they queued), and `!weblink` DMs the links only to DJs. `!djrole off` restores it.
- [ ] A member without Manage Server cannot run `!theme`, `!djrole` or `!language`.

**Server settings**

- [ ] `!theme night` changes the page's default; `!theme edit` DMs an editor link; a custom theme with a logo saves and shows to every viewer; `!theme reset`.
- [ ] `!language fr`: replies, the status message, `!help` and the page switch to French (check accents and spacing before `:` and `!`); `!language reset`.

Features still on their own branches (audio filters, 24/7 stay, queue limits, vote skip, Last.fm, backups) get their lines here when they are merged.

## Building these docs

The documentation is written in Markdown under `docs/source/` and built with Sphinx, MyST-Parser and the Furo theme. It is published on [Read the Docs](https://dj-snake.readthedocs.io). CI builds it with warnings treated as errors, so build it locally before you push a docs change:

```bash
python3 -m venv docs-venv && source docs-venv/bin/activate
pip install -r docs/requirements.txt
sphinx-build -W --keep-going -b html docs/source docs/_build/html
```

Then open `docs/_build/html/index.html`. The changelog page includes `CHANGELOG.md` directly, so do not copy it.

## Pull requests

Keep them focused, one change each, and update the docs (`README.md`, `docs/source/`, `CLAUDE.md`) when behaviour or configuration changes. History uses gitmoji-style prefixes (for example a sparkles emoji for features), then a short imperative summary.

Never commit `.env`, a `token` file or any real token, hostname or IP.

## Releasing

Move the `[Unreleased]` notes in `CHANGELOG.md` under a new version, commit, then tag `vX.Y.Z` and push the tag. The release workflow builds and pushes the multi-arch `ghcr.io/theroxat/dj_snake` image and creates the GitHub Release.

After changing `web/index.html` (or `docs/demo/shim.js`), run `python3 docs/demo/build.py` and commit the regenerated `docs/demo/player.html`.
