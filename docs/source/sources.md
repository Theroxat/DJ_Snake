# More sources

Besides YouTube and [Spotify](spotify.md), `!play` and the clip page's add box accept SoundCloud, Bandcamp, Deezer and Apple Music links, and [live streams](#live-streams) (YouTube lives, Twitch channels, internet radios). None of them needs an account or an API key.

## SoundCloud and Bandcamp

yt-dlp reads these natively, so the audio comes from the platform itself.

```text
!play https://soundcloud.com/forss/flickermood
!play https://soundcloud.com/forss/sets/soulhack
!play https://artist.bandcamp.com/album/some-album
```

- A track link queues that track. A SoundCloud set (`/user/sets/name`) or a Bandcamp album (`/album/...`) queues up to `MAX_PLAYLIST_SIZE` tracks, like a YouTube playlist.
- These tracks have no video: the clip page shows the artwork full size in the player instead of a black frame.
- SponsorBlock only knows YouTube, so nothing is cut from these tracks. Loudness normalisation and silence trimming still apply.
- The clip page's add box only accepts `soundcloud.com` and `*.bandcamp.com` addresses. SoundCloud short links (`on.soundcloud.com`) and Bandcamp pages on a custom domain are refused there.

## Deezer and Apple Music

Like Spotify, these links are only used to read the track list (title, artists, duration). Each track is then matched to a YouTube video with the same scoring as Spotify, and the match is downloaded. Nothing is played from Deezer or Apple Music.

```text
!play https://www.deezer.com/en/album/302127
!play https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb
```

- **Deezer** tracks, albums and playlists are read from Deezer's public API. Short `link.deezer.com` links are refused: open them and copy the `deezer.com` address.
- **Apple Music** songs and albums (an album link with `?i=` is that one song) are read from Apple's public iTunes lookup API. Playlists are read from the public playlist page. Copy the address as the browser shows it: a link with a wrong name part is redirected and refused.
- Lists are capped at `MAX_PLAYLIST_SIZE`. Progress is shown as for Spotify, including `not found on YouTube` for tracks without a match.
- Private playlists cannot be read, and a service that changes its page or API can make a link unreadable until the bot is updated.

## Live streams

YouTube lives, Twitch channels and internet radios cannot be downloaded, so they are played as they come, reconnecting by themselves after a network hiccup.

```text
!play https://www.youtube.com/watch?v=jfKfPfyJRdk
!play https://www.twitch.tv/somechannel
!play https://icecast.radiofrance.fr/fip-midfi.mp3
!play https://somafm.com/groovesalad.pls
```

- **YouTube**: any video link that is live right now (also `youtube.com/live/<id>`). A live that has not started yet is refused; a finished one is a normal video.
- **Twitch**: a channel link, `twitch.tv/<channel>`, while the channel is live. Videos and clips are not accepted.
- **Internet radios**: an Icecast or Shoutcast stream, or a `.pls` / `.m3u` file pointing to one. The link must end in a stream or playlist file (`.mp3`, `.aac`, `.ogg`, `.opus`, `.flac`, `.m3u`, `.pls`, or a name like `groovesalad-128-mp3`) or a usual mount point (`/stream`, `/listen`, `/live`, `/;`). The bot checks it before queueing it: it must answer with an audio content type, and its address must be public (links to private or local addresses are refused, and so are redirects to them). HLS radios (`.m3u8`) are not supported: use the station's direct MP3, AAC or Ogg link. The bot reads the radio itself, connected to the address it checked, and hands the audio to FFmpeg. The station name comes from the stream's `icy-name`, and the song playing now (`StreamTitle`) is read every 30 seconds and shown on the clip page and in the status message.
- A live item plays until it is skipped, `!stop` is used, the stream ends or it has played for `LIVE_MAX_HOURS` (4 by default, pauses not counted); then the queue goes on. Live streams are not counted in the listening stats or scrobbled to Last.fm.
- There is nothing to seek in: the clip page shows a LIVE badge instead of the progress bar, and `!seek` or the page's seek answer that a live stream has no position. Loop track does not replay a stream that ended, and no crossfade is made into or out of a live item.
- Loudness is evened out on the fly (one-pass normalisation) instead of the measured automix; there is no SponsorBlock cut and no silence trimming.
- The clip page shows the stream's artwork (the YouTube or Twitch thumbnail) or an "On air" card, never the live video. Lyrics are not looked up.
- Listening time counts in the stats, with no duration.

DJ Snake is not affiliated with or endorsed by SoundCloud, Bandcamp, Deezer, Apple or Twitch.
