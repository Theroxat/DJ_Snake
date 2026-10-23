# More sources

Besides YouTube and [Spotify](spotify.md), `!play` and the clip page's add box accept SoundCloud, Bandcamp, Deezer and Apple Music links. None of them needs an account or an API key.

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

DJ Snake is not affiliated with or endorsed by SoundCloud, Bandcamp, Deezer or Apple.
