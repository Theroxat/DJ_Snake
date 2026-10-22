# Spotify support

Paste a Spotify playlist, album or track into `!play` or the clip page's add box and DJ Snake plays it, with no Spotify account and no API key.

```
!play https://open.spotify.com/playlist/4rT7xKq2LmNb9VcZ0aYdEw
```

## How it works

1. The bot reads the track list (title, artists, duration) from the link. It never plays or downloads anything from Spotify.
2. It searches YouTube for each track and scores the results: the duration must be close, and the title and artist must match.
3. Official music videos come first (the clip page shows the video), then official audio and "- Topic" uploads. Live, cover, remix, karaoke, nightcore and sped-up versions are skipped unless the Spotify title says so.
4. Tracks load in the background like a YouTube playlist, up to `MAX_PLAYLIST_SIZE`. Songs with no good match are skipped and counted in the status message, for example "28/30 tracks loaded (2 not found on YouTube)".

Once queued, the tracks are ordinary YouTube tracks.

## Accepted links

`open.spotify.com` addresses ending in `/playlist/`, `/album/` or `/track/` (with optional `intl-xx` and `embed` path parts), and `spotify:playlist:...` style URIs. Short `spotify.link` links are refused: open them and copy the `open.spotify.com` address.

## Limits

- Without credentials, the list comes from Spotify's public embed page, which shows at most 100 tracks of a playlist or album.
- Private playlists cannot be read, with or without credentials.
- The match can occasionally be the wrong version of a song.

## Longer playlists with the Web API

For lists longer than 100 tracks, create an app in the [Spotify developer dashboard](https://developer.spotify.com/dashboard) and set both variables in `.env`:

```bash
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
```

The bot then uses the Spotify Web API (client credentials flow). If the API refuses a playlist (Spotify hides its own editorial playlists from new apps), it falls back to the embed page. The number of tracks queued is still capped by `MAX_PLAYLIST_SIZE`.

## Spotify errors

| Message cause | What it means |
| --- | --- |
| Not found | The playlist is private or does not exist. |
| Empty | The link has no tracks. |
| Unreadable | Spotify is unreachable or changed its page. Try again later. |

DJ Snake is not affiliated with or endorsed by Spotify.
