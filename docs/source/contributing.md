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

CI runs these on Python 3.10 to 3.12, and a change must pass them:

```bash
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
black --check --diff .
python -c "import bot"
pytest -q
```

Black uses its default 88-column limit.

## Tests

Run `pytest` after installing `requirements-dev.txt`. The suite in `tests/` needs no network, Discord or YouTube: voice, channels and FFmpeg are faked, and the few tests that need the real `ffmpeg` binary are skipped without it. Add tests for any behaviour you change.

Playback itself still needs a hand test: run the bot against a test server and try your change, for example `!play <youtube url>`, plus the clip page if you touched `web/`.

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
