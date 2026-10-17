# Security Policy

## Supported versions

Only the latest `main` is supported.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting: on the repository, go to Security, then "Report a vulnerability". Include steps to reproduce and the impact.

## Scope notes

- The clip page link is a bearer token. Anyone holding it can control that server's queue, and it expires after `SESSION_IDLE_HOURS` of inactivity. Expose the page only through a proxy or tunnel you trust.
- Never commit `.env`, a `token` file or your Discord token. If a token leaks, reset it in the Discord Developer Portal.
- The bot accepts only YouTube URLs and rebuilds them in canonical form before passing them to yt-dlp.
