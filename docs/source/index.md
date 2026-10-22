# DJ Snake

**A self-hosted Discord music bot that plays YouTube or Spotify links in your voice channel, with a live clip page that shows the video in sync.**

[![Live demo: DJ Snake joins a voice channel, posts the clip link, then the clip page plays a synced video](../screenshots/clip-page.png)](https://theroxat.github.io/DJ_Snake/demo/)

[**Try the live demo**](https://theroxat.github.io/DJ_Snake/demo/) (a simulated Discord channel and a real clip page, running in your browser) or visit the [project page](https://theroxat.github.io/DJ_Snake/).

## What you get

- **Spotify playlists, albums and tracks.** Paste a link and the bot finds every song on YouTube, preferring the official music video, with no Spotify account or API key needed ([details](spotify.md)).
- **YouTube URLs and playlists.** Tracks are fully downloaded before they play (no streaming).
- **An independent queue per server.**
- **A live clip page**: a muted video synced with the audio, plus web controls (next, previous, pause, resume, seek, jump, remove, reorder, add) ([details](clip-page.md)).
- **Automix, always on**: SponsorBlock `music_offtopic` segments are cut, loudness is normalised to -14 LUFS, leading and trailing silence is trimmed, and tracks fade in and out.
- **Optional crossfade** between tracks.
- **Listening stats** per server on the clip page: top tracks and artists, the most skipped track, hours played, sessions, peak listeners and a day-by-hour heatmap.
- **Themable clip page**: eight presets plus a custom look with your colors, name and logo.
- **One live status message per session**, edited in place instead of spamming the channel.
- **Self-healing YouTube access**: yt-dlp upgrades itself on start, and a PO token sidecar handles YouTube's bot check ([details](youtube.md)).

## Where to start

1. [Install the bot](installation.md) with Docker Compose in a few minutes.
2. Tune it with the [configuration reference](configuration.md).
3. Learn the [commands](commands.md) and the [clip page](clip-page.md).
4. If something goes wrong, see [Troubleshooting](troubleshooting.md).

DJ Snake is released under the [MIT license](https://github.com/Theroxat/DJ_Snake/blob/main/LICENSE). The source code is on [GitHub](https://github.com/Theroxat/DJ_Snake).

```{toctree}
:caption: Using DJ Snake
:hidden:
:maxdepth: 2

installation
configuration
commands
clip-page
spotify
youtube
troubleshooting
```

```{toctree}
:caption: Project
:hidden:
:maxdepth: 2

contributing
architecture
changelog
```
