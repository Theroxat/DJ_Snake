---
name: Bug report
about: Report something that does not work
title: '[BUG] '
labels: 'bug'
assignees: ''
---

**What happened**
What you did, what you expected, what you got.

**Steps to reproduce**
1. `!play <url>` (or the action on the clip page)
2. ...

**Deployment**
- [ ] Docker Compose
- [ ] Local (Python version: )

**yt-dlp version**
`docker exec dj-snake python -m yt_dlp --version` (or `python -m yt_dlp --version` locally)

**Logs**
`docker compose logs --tail 100 dj-snake`. Redact your Discord token and any private URLs.
