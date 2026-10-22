# Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/Theroxat/DJ_Snake). For an architecture overview and the module map, read [CLAUDE.md](CLAUDE.md). The bot lives in the `djsnake/` package; `bot.py` is the entry point.

## Setup

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

## Testing

Run `pytest` (after `pip install -r requirements-dev.txt`). The suite in `tests/` needs no network, Discord or YouTube: voice, channels and FFmpeg are faked, and the few tests that need the real `ffmpeg` binary are skipped without it. Add tests for any behaviour you change.

Playback itself still needs a hand test: run the bot against a test server and try your change, for example `!play <youtube url>`, plus the clip page if you touched `web/`.

## Pull requests

Keep them focused, one change each, and update the docs (`README.md`, `CLAUDE.md`) when behaviour or configuration changes. History uses gitmoji-style prefixes (for example a sparkles emoji for features), then a short imperative summary.

Never commit `.env`, a `token` file or any real token, hostname or IP.
