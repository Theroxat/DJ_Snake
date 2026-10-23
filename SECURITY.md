# Security Policy

## Supported versions

Only the latest `main` is supported.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting: on the repository, go to Security, then "Report a vulnerability". Include steps to reproduce and the impact.

## Scope notes

- The clip page links are bearer tokens. Anyone holding the control link can control that server's queue; the view-only link can only watch (the server refuses its controls). Both expire after `SESSION_IDLE_HOURS` of inactivity. Set a DJ role (`!djrole`) or `WEB_CONTROL=dj` to keep the control link out of public channels. Expose the page only through a proxy or tunnel you trust.
- Never commit `.env`, a `token` file or your Discord token. If a token leaks, reset it in the Discord Developer Portal.
- `!play` and the clip page's add box accept only YouTube, SoundCloud, Bandcamp, Spotify, Deezer and Apple Music links and rebuild them in canonical form before passing anything to yt-dlp or fetching metadata; other hosts are refused. Metadata requests do not follow redirects.
