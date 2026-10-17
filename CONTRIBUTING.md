# Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/Theroxat/DJ_Snake). For an architecture overview, read [CLAUDE.md](CLAUDE.md). All bot logic lives in `bot.py`.

## Setup

```bash
git clone https://github.com/Theroxat/DJ_Snake.git && cd DJ_Snake
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt black flake8
cp .env.example .env   # set DISCORD_TOKEN and POT_BASE_URL
```

You need `ffmpeg` and `deno` on your `PATH`.

## Checks

CI runs these on Python 3.10 to 3.12, and a change must pass them:

```bash
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
black --check --diff .
python -c "import bot"
```

Black uses its default 88-column limit.

## Testing

There is no test suite. Run the bot against a test server and try your change by hand, for example `!play <youtube url>`, plus the clip page if you touched `web/`.

## Pull requests

Keep them focused, one change each, and update the docs (`README.md`, `CLAUDE.md`) when behaviour or configuration changes. History uses gitmoji-style prefixes (for example a sparkles emoji for features), then a short imperative summary.

Never commit `.env`, a `token` file or any real token, hostname or IP.
